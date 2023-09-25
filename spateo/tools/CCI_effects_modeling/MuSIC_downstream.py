"""
Additional functionalities to characterize signaling patterns from spatial transcriptomics

These include:
    - prediction of the effects of spatial perturbation on gene expression- this can include the effect of perturbing
    known regulators of ligand/receptor expression or the effect of perturbing the ligand/receptor itself.
    - following spatially-aware regression (or a sequence of spatially-aware regressions), combine regression results
    with data such that each cell can be associated with region-specific coefficient(s).
    - following spatially-aware regression (or a sequence of spatially-aware regressions), overlay the directionality
    of the predicted influence of the ligand on downstream expression.
"""
import argparse
import itertools
import math
import os
import re
from collections import Counter
from typing import List, Literal, Optional, Tuple, Union

import anndata
import igviz as ig
import matplotlib
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import plotly
import scipy.sparse
import scipy.stats
import seaborn as sns
import xarray as xr
from joblib import Parallel, delayed
from matplotlib import rcParams
from mpi4py import MPI
from pysal import explore, lib
from scipy.stats import pearsonr, spearmanr, ttest_1samp
from sklearn.decomposition import TruncatedSVD
from sklearn.metrics import f1_score, mean_squared_error, roc_auc_score
from sklearn.preprocessing import normalize

from ...configuration import config_spateo_rcParams
from ...logging import logger_manager as lm
from ...plotting.static.utils import save_return_show_fig_utils
from ...preprocessing.transform import log1p
from ..dimensionality_reduction import find_optimal_pca_components, pca_fit
from .MuSIC import MuSIC
from .regression_utils import multitesting_correction, permutation_testing, wald_test
from .SWR_mpi import define_spateo_argparse


# ---------------------------------------------------------------------------------------------------
# Statistical testing, correlated differential expression analysis
# ---------------------------------------------------------------------------------------------------
class MuSIC_Interpreter(MuSIC):
    """
    Interpretation and downstream analysis of spatially weighted regression models.

    Args:
        comm: MPI communicator object initialized with mpi4py, to control parallel processing operations
        parser: ArgumentParser object initialized with argparse, to parse command line arguments for arguments
            pertinent to modeling.
        args_list: If parser is provided by function call, the arguments to parse must be provided as a separate
            list. It is recommended to use the return from :func `define_spateo_argparse()` for this.
    """

    def __init__(self, comm: MPI.Comm, parser: argparse.ArgumentParser, args_list: Optional[List[str]] = None):
        super().__init__(comm, parser, args_list, verbose=False)

        self.k = self.arg_retrieve.top_k_receivers

        self.logger.info("Gathering all information from preliminary L:R model...")
        # Coefficients:
        if not self.set_up:
            self.logger.info(
                "Running :func `SWR._set_up_model()` to organize predictors and targets for downstream "
                "analysis now..."
            )
            self._set_up_model()
            # self.logger.info("Finished preprocessing, getting fitted coefficients and standard errors.")

        # Dictionary containing coefficients:
        self.coeffs, self.standard_errors = self.return_outputs()
        self.coeffs = self.comm.bcast(self.coeffs, root=0)
        self.standard_errors = self.comm.bcast(self.standard_errors, root=0)
        # Design matrix:
        self.design_matrix = pd.read_csv(
            os.path.join(os.path.splitext(self.output_path)[0], "design_matrix", "design_matrix.csv"), index_col=0
        )

        self.predictions = self.predict(coeffs=self.coeffs)
        self.predictions = self.comm.bcast(self.predictions, root=0)

        chunk_size = int(math.ceil(float(len(range(self.n_samples))) / self.comm.size))
        self.x_chunk = np.arange(self.n_samples)[self.comm.rank * chunk_size : (self.comm.rank + 1) * chunk_size]
        self.x_chunk = self.comm.bcast(self.x_chunk, root=0)

        # Save directory:
        parent_dir = os.path.dirname(self.output_path)
        if not os.path.exists(os.path.join(parent_dir, "significance")):
            os.makedirs(os.path.join(parent_dir, "significance"))

        # Arguments for cell type coupling computation:
        self.filter_targets = self.arg_retrieve.filter_targets
        self.filter_target_threshold = self.arg_retrieve.filter_target_threshold

        # Get targets for the downstream ligand(s), receptor(s), target(s), etc. to use for analysis:
        self.ligand_for_downstream = self.arg_retrieve.ligand_for_downstream
        self.receptor_for_downstream = self.arg_retrieve.receptor_for_downstream
        self.pathway_for_downstream = self.arg_retrieve.pathway_for_downstream
        self.target_for_downstream = self.arg_retrieve.target_for_downstream
        self.sender_ct_for_downstream = self.arg_retrieve.sender_ct_for_downstream
        self.receiver_ct_for_downstream = self.arg_retrieve.receiver_ct_for_downstream

        # Other downstream analysis-pertinent argparse arguments:
        self.cci_degs_model_interactions = self.arg_retrieve.cci_degs_model_interactions
        self.no_cell_type_markers = self.arg_retrieve.no_cell_type_markers
        self.compute_pathway_effect = self.arg_retrieve.compute_pathway_effect
        self.diff_sending_or_receiving = self.arg_retrieve.diff_sending_or_receiving

    def compute_coeff_significance(self, method: str = "fdr_bh", significance_threshold: float = 0.05):
        """Computes local statistical significance for fitted coefficients.

        Args:
             method: Method to use for correction. Available methods can be found in the documentation for
                statsmodels.stats.multitest.multipletests(), and are also listed below (in correct case) for
                convenience:
                - Named methods:
                    - bonferroni
                    - sidak
                    - holm-sidak
                    - holm
                    - simes-hochberg
                    - hommel
                - Abbreviated methods:
                    - fdr_bh: Benjamini-Hochberg correction
                    - fdr_by: Benjamini-Yekutieli correction
                    - fdr_tsbh: Two-stage Benjamini-Hochberg
                    - fdr_tsbky: Two-stage Benjamini-Krieger-Yekutieli method
            significance_threshold: p-value (or q-value) needed to call a parameter significant.

        Returns:
            is_significant: Dataframe of identical shape to coeffs, where each element is True or False if it meets the
            threshold for significance
            pvalues: Dataframe of identical shape to coeffs, where each element is a p-value for that instance of that
                feature
            qvalues: Dataframe of identical shape to coeffs, where each element is a q-value for that instance of that
                feature
        """

        for target in self.coeffs.keys():
            # Get coefficients and standard errors for this key
            coef = self.coeffs[target]
            columns = [col for col in coef.columns if col.startswith("b_") and "intercept" not in col]
            coef = coef[columns]
            coef = self.comm.bcast(coef, root=0)
            se = self.standard_errors[target]
            se = self.comm.bcast(se, root=0)
            se_feature_match = [c.replace("se_", "") for c in se.columns]

            # Parallelize computations over observations and features:
            local_p_values_all = np.zeros((len(self.x_chunk), self.n_features))

            # Compute p-values for local observations and features
            for i, obs_index in enumerate(self.x_chunk):
                for j in range(self.n_features):
                    if self.feature_names[j] in se_feature_match:
                        if se.iloc[obs_index][f"se_{self.feature_names[j]}"] == 0:
                            local_p_values_all[i, j] = 1
                        else:
                            local_p_values_all[i, j] = wald_test(
                                coef.iloc[obs_index][f"b_{self.feature_names[j]}"],
                                se.iloc[obs_index][f"se_{self.feature_names[j]}"],
                            )
                    else:
                        local_p_values_all[i, j] = 1

            # Collate p-values from all processes:
            p_values_all = self.comm.gather(local_p_values_all, root=0)

            if self.comm.rank == 0:
                p_values_all = np.concatenate(p_values_all, axis=0)
                p_values_df = pd.DataFrame(p_values_all, index=self.sample_names, columns=self.feature_names)
                # Multiple testing correction for each observation:
                qvals = np.zeros_like(p_values_all)
                for i in range(p_values_all.shape[0]):
                    qvals[i, :] = multitesting_correction(
                        p_values_all[i, :], method=method, alpha=significance_threshold
                    )
                q_values_df = pd.DataFrame(qvals, index=self.sample_names, columns=self.feature_names)

                # Significance:
                is_significant_df = q_values_df < significance_threshold

                # Save dataframes:
                parent_dir = os.path.dirname(self.output_path)
                p_values_df.to_csv(os.path.join(parent_dir, "significance", f"{target}_p_values.csv"))
                q_values_df.to_csv(os.path.join(parent_dir, "significance", f"{target}_q_values.csv"))
                is_significant_df.to_csv(os.path.join(parent_dir, "significance", f"{target}_is_significant.csv"))

    def compute_diagnostics(self):
        """
        For true and predicted gene expression, compute and generate plots of various diagnostics, including the
        Pearson correlation, Spearman correlation and root mean-squared-error (RMSE).
        """
        # Plot title:
        file_name = os.path.splitext(os.path.basename(self.adata_path))[0]

        parent_dir = os.path.dirname(self.output_path)
        pred_path = os.path.join(parent_dir, "predictions.csv")

        predictions = pd.read_csv(pred_path, index_col=0)
        all_genes = predictions.columns
        width = 0.5 * len(all_genes)
        pred_vals = predictions.values

        # Pearson and Spearman dictionary for all cells:
        pearson_dict = {}
        spearman_dict = {}
        # Pearson and Spearman dictionary for only the expressing subset of cells:
        nz_pearson_dict = {}
        nz_spearman_dict = {}

        for i, gene in enumerate(all_genes):
            y = self.adata[:, gene].X.toarray().reshape(-1)
            music_results_target = pred_vals[:, i]

            # Remove index of the largest predicted value (to mitigate sensitivity of these metrics to outliers):
            outlier_index = np.where(np.max(music_results_target))[0]
            music_results_target_to_plot = np.delete(music_results_target, outlier_index)
            y_plot = np.delete(y, outlier_index)

            # Indices where target is nonzero:
            nonzero_indices = y_plot != 0

            rp, _ = pearsonr(y_plot, music_results_target_to_plot)
            r, _ = spearmanr(y_plot, music_results_target_to_plot)

            rp_nz, _ = pearsonr(y_plot[nonzero_indices], music_results_target_to_plot[nonzero_indices])
            r_nz, _ = spearmanr(y_plot[nonzero_indices], music_results_target_to_plot[nonzero_indices])

            pearson_dict[gene] = rp
            spearman_dict[gene] = r
            nz_pearson_dict[gene] = rp_nz
            nz_spearman_dict[gene] = r_nz

        # Mean of diagnostic metrics:
        mean_pearson = sum(pearson_dict.values()) / len(pearson_dict.values())
        mean_spearman = sum(spearman_dict.values()) / len(spearman_dict.values())
        mean_nz_pearson = sum(nz_pearson_dict.values()) / len(nz_pearson_dict.values())
        mean_nz_spearman = sum(nz_spearman_dict.values()) / len(nz_spearman_dict.values())

        data = []
        for gene in pearson_dict.keys():
            data.append(
                {
                    "Gene": gene,
                    "Pearson coefficient": pearson_dict[gene],
                    "Spearman coefficient": spearman_dict[gene],
                    "Pearson coefficient (expressing cells)": nz_pearson_dict[gene],
                    "Spearman coefficient (expressing cells)": nz_spearman_dict[gene],
                }
            )
        # Color palette:
        colors = {
            "Pearson coefficient": "#FF7F00",
            "Spearmann coefficient": "#87CEEB",
            "Pearson coefficient (expressing cells)": "#0BDA51",
            "Spearmann coefficient (expressing cells)": "#FF6961",
        }
        df = pd.DataFrame(data)

        # Plot Pearson correlation barplot:
        sns.set(font_scale=2)
        sns.set_style("white")
        plt.figure(figsize=(width, 6))
        plt.xticks(rotation="vertical")
        ax = sns.barplot(
            data=df,
            x="Gene",
            y="Pearson coefficient",
            palette=colors["Pearson coefficient"],
            edgecolor="black",
            dodge=True,
        )

        # Mean line:
        line_style = "--"  # Specify the line style (e.g., "--" for dotted)
        line_thickness = 2  # Specify the line thickness
        ax.axhline(mean_pearson, color="black", linestyle=line_style, linewidth=line_thickness)

        # Update legend:
        legend_label = f"Mean: {mean_pearson}"
        handles, labels = ax.get_legend_handles_labels()
        handles.append(plt.Line2D([0], [0], color="black", linewidth=line_thickness, linestyle=line_style))
        labels.append(legend_label)
        ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1, 0.5))

        plt.title(f"Pearson correlation {file_name}")
        plt.tight_layout()
        plt.show()

        # Plot Spearman correlation barplot:
        plt.figure(figsize=(width, 6))
        plt.xticks(rotation="vertical")
        ax = sns.barplot(
            data=df,
            x="Gene",
            y="Spearman coefficient",
            palette=colors["Spearman coefficient"],
            edgecolor="black",
            dodge=True,
        )

        # Mean line:
        ax.axhline(mean_spearman, color="black", linestyle=line_style, linewidth=line_thickness)

        # Update legend:
        legend_label = f"Mean: {mean_spearman}"
        handles, labels = ax.get_legend_handles_labels()
        handles.append(plt.Line2D([0], [0], color="black", linewidth=line_thickness, linestyle=line_style))
        labels.append(legend_label)
        ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1, 0.5))

        plt.title(f"Spearman correlation {file_name}")
        plt.tight_layout()
        plt.show()

        # Plot Pearson correlation barplot (expressing cells):
        plt.figure(figsize=(width, 6))
        plt.xticks(rotation="vertical")
        ax = sns.barplot(
            data=df,
            x="Gene",
            y="Pearson coefficient (expressing cells)",
            palette=colors["Pearson coefficient (expressing cells)"],
            edgecolor="black",
            dodge=True,
        )

        # Mean line:
        ax.axhline(mean_nz_pearson, color="black", linestyle=line_style, linewidth=line_thickness)

        # Update legend:
        legend_label = f"Mean: {mean_nz_pearson}"
        handles, labels = ax.get_legend_handles_labels()
        handles.append(plt.Line2D([0], [0], color="black", linewidth=line_thickness, linestyle=line_style))
        labels.append(legend_label)
        ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1, 0.5))

        plt.title(f"Pearson correlation (expressing cells) {file_name}")
        plt.tight_layout()
        plt.show()

        # Plot Spearman correlation barplot (expressing cells):
        plt.figure(figsize=(width, 6))
        plt.xticks(rotation="vertical")
        ax = sns.barplot(
            data=df,
            x="Gene",
            y="Spearman coefficient (expressing cells)",
            palette=colors["Spearman coefficient (expressing cells)"],
            edgecolor="black",
            dodge=True,
        )

        # Mean line:
        ax.axhline(mean_nz_spearman, color="black", linestyle=line_style, linewidth=line_thickness)

        # Update legend:
        legend_label = f"Mean: {mean_nz_spearman}"
        handles, labels = ax.get_legend_handles_labels()
        handles.append(plt.Line2D([0], [0], color="black", linewidth=line_thickness, linestyle=line_style))
        labels.append(legend_label)
        ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1, 0.5))

        plt.title(f"Spearman correlation (expressing cells) {file_name}")
        plt.tight_layout()
        plt.show()

    def visualize_enriched_interactions(
        self,
        target_subset: Optional[List[str]] = None,
        interaction_subset: Optional[List[str]] = None,
        cell_types: Optional[List[str]] = None,
        metric: Literal["number", "multiplicity", "proportion", "specificity", "mean", "fc", "fc_qvals"] = "fc",
        normalize: bool = True,
        plot_significant: bool = False,
        metric_threshold: float = 0.05,
        cut_pvals: float = -5,
        fontsize: Union[None, int] = None,
        figsize: Union[None, Tuple[float, float]] = None,
        center: Optional[float] = None,
        cmap: str = "Reds",
        save_show_or_return: Literal["save", "show", "return", "both", "all"] = "show",
        save_kwargs: Optional[dict] = {},
        save_df: bool = False,
    ):
        """Given the target gene of interest, identify interaction features that are enriched for particular targets.
        Visualized in heatmap form.

        Args:
            target_subset: List of targets to consider. If None, will use all targets used in model fitting.
            interaction_subset: List of interactions to consider. If None, will use all interactions used in model.
            cell_types: Can be used to restrict the enrichment analysis to only cells of a particular type. If given,
                will search for cell types in "group_key" attribute from model initialization.
            metric: Metric to display on plot. For all plot variants, the color will be determined by a combination
            of the size & magnitude of the effect. Options:
                - "number": Number of cells for which the interaction is predicted to have nonzero effect
                - "multiplicity": For each interaction/target combination, gets the average number of additional
                    interactions that are predicted in cells expressing that target
                - "proportion": Percentage of interactions predicted to have nonzero effect over the number of cells
                    that express each target.
                - "specificity": Number of target-expressing cells for which a particular interaction is predicted to
                    have nonzero effect over the total number of cells for which a particular interaction is
                    present in (including target-expressing and non-expressing cells). Essentially, measures the
                    degree to which an interaction is coupled to a particular target.
                - "mean": Average effect size over all target-expressing cells.
                - "fc": Fold change in mean expression of target-expressing cells with and without each specified
                    interaction. Way of inferring that interaction may actually be repressive rather than activatory.
                - "fc_qvals": Log-transformed significance of the fold change.
            normalize: Whether to minmax scale the metric values. If True, will apply this scaling over all elements
                of the array. Only used for 'metric' = "number", "proportion" or "specificity".
            plot_significant: Whether to include only significant predicted interactions in the plot and metric
                calculation.
            metric_threshold: Optional threshold for 'metric' used to filter plot elements. Any interactions below
                this threshold will not be color coded. Will use 0.05 by default. Should be between 0 and 1. For
                'metric' = "fc", this threshold will be interpreted as a distance from a fold-change of 1.
            cut_pvals: For metric = "fc_qvals", the q-values are log-transformed. Any log10-transformed q-value that is
                below this will be clipped to this value.
            fontsize: Size of font for x and y labels.
            figsize: Size of figure.
            center: Optional, determines position of the colormap center. Between 0 and 1.
            cmap: Colormap to use for heatmap. If metric is "number", "proportion", "specificity", the bottom end of
                the range is 0. It is recommended to use a sequential colormap (e.g. "Reds", "Blues", "Viridis",
                etc.). For metric = "fc", if a divergent colormap is not provided, "seismic" will automatically be
                used.
            save_show_or_return: Whether to save, show or return the figure.
                If "both", it will save and plot the figure at the same time. If "all", the figure will be saved,
                displayed and the associated axis and other object will be return.
            save_kwargs: A dictionary that will passed to the save_fig function.
                By default it is an empty dictionary and the save_fig function will use the
                {"path": None, "prefix": 'scatter', "dpi": None, "ext": 'pdf', "transparent": True, "close": True,
                "verbose": True} as its parameters. Otherwise you can provide a dictionary that properly modifies those
                keys according to your needs.
            save_df: Set True to save the metric dataframe in the end
        """
        logger = lm.get_main_logger()
        config_spateo_rcParams()
        # But set display DPI to 300:
        plt.rcParams["figure.dpi"] = 300

        if save_df:
            output_folder = os.path.join(os.path.dirname(self.output_path), "analyses")
            if not os.path.exists(output_folder):
                os.makedirs(output_folder)

        # Check inputs:
        if metric not in ["number", "proportion", "specificity", "mean", "fc", "fc_qvals"]:
            raise ValueError(
                f"Unrecognized metric {metric}. Options are 'number', 'proportion', 'specificity', 'mean', "
                f"'fc' or 'fc_qvals'."
            )

        if cell_types is None:
            adata = self.adata.copy()
        else:
            adata = self.adata[self.adata.obs[self.group_key].isin(cell_types)].copy()

        all_targets = list(self.coeffs.keys())
        targets = all_targets if target_subset is None else target_subset
        feature_names = [feat for feat in self.feature_names if feat != "intercept"]
        df = pd.DataFrame(0, index=feature_names, columns=targets)

        # Colormap can be divergent for any option-
        diverging_colormaps = [
            "PiYG",
            "PRGn",
            "BrBG",
            "PuOr",
            "RdGy",
            "RdBu",
            "RdYlBu",
            "RdYlGn",
            "Spectral",
            "coolwarm",
            "bwr",
            "seismic",
        ]

        # For metric = fold change, significance of the fold-change:
        if metric == "fc" or metric == "fc_qvals":
            df_pvals = pd.DataFrame(1, index=feature_names, columns=targets)
            if metric == "fc":
                if cmap not in diverging_colormaps:
                    logger.info("For metric fold-change, colormap should be divergent: using 'seismic'.")
                    cmap = "seismic"
        if metric != "fc":
            sequential_colormaps = [
                "Blues",
                "BuGn",
                "BuPu",
                "GnBu",
                "Greens",
                "Greys",
                "Oranges",
                "OrRd",
                "PuBu",
                "PuBuGn",
                "PuRd",
                "Purples",
                "RdPu",
                "Reds",
                "YlGn",
                "YlGnBu",
                "YlOrBr",
                "YlOrRd",
                "afmhot",
                "autumn",
                "bone",
                "cool",
                "copper",
                "gist_heat",
                "gray",
                "hot",
                "pink",
                "spring",
                "summer",
                "winter",
                "viridis",
                "plasma",
                "inferno",
                "magma",
                "cividis",
            ]
            if cmap not in sequential_colormaps and cmap not in diverging_colormaps:
                logger.info(f"For metric {metric}, colormap should be sequential: using 'viridis'.")
                cmap = "viridis"

        if fontsize is None:
            fontsize = rcParams.get("font.size")
        if figsize is None:
            # Set figure size based on the number of interaction features and targets:
            m = len(feature_names) * 40 / 300
            n = len(targets) * 40 / 300
            figsize = (n, m)

        for target in self.coeffs.keys():
            # Get coefficients for this key
            if interaction_subset is not None:
                if target not in interaction_subset:
                    continue
            coef = self.coeffs[target]
            columns = [col for col in coef.columns if col.startswith("b_") and "intercept" not in col]
            feat_names_target = [col.replace("b_", "") for col in columns]

            # For fold-change, significance will be incorporated post-calculation:
            if plot_significant and metric != "fc":
                # Adjust coefficients array to include only the significant coefficients:
                # Try to load significance matrix, and if not found, compute it:
                try:
                    parent_dir = os.path.dirname(self.output_path)
                    is_significant_df = pd.read_csv(
                        os.path.join(parent_dir, "significance", f"{target}_is_significant.csv"), index_col=0
                    )
                except:
                    self.logger.info(
                        "Could not find significance matrix. Computing it now with the "
                        "Benjamini-Hochberg correction and significance threshold of 0.05..."
                    )
                    self.compute_coeff_significance()
                    parent_dir = os.path.dirname(self.output_path)
                    is_significant_df = pd.read_csv(
                        os.path.join(parent_dir, "significance", f"{target}_is_significant.csv"), index_col=0
                    )
                # Take the subset of the significance matrix that applies to the columns used for this target:
                is_significant_df = is_significant_df.loc[:, feat_names_target]
                # Convolve coefficients with significance matrix:
                coef = coef * is_significant_df.values

            if metric == "number":
                # Compute number of nonzero interactions for each feature:
                n_nonzero_interactions = np.sum(coef != 0, axis=0)
                n_nonzero_interactions.index = [idx.replace("b_", "") for idx in n_nonzero_interactions.index]
                # Compute proportion:
                df.loc[feat_names_target, target] = n_nonzero_interactions
            elif metric == "multiplicity":
                multiplicity_series = pd.Series()
                # Indices of target-expressing cells:
                target_expr_cells_indices = np.where(adata[:, target].X.toarray() != 0)[0]
                # Extract only the rows of coef that correspond to target-expressing cells:
                coef_target_expr = coef.iloc[target_expr_cells_indices, :]
                # For each interaction (each column), subset to nonzero rows:
                for col in coef_target_expr.columns:
                    nonzero_rows = coef_target_expr[coef_target_expr[col] != 0]
                    # Count the number of predicted cooccurring interactions (exclude the current column and count
                    # the number of nonzeros in each row):
                    count_nonzero = (nonzero_rows.drop(columns=[col]) != 0).sum(axis=1)
                    # Compute the multiplicity of the interaction as the mean number of cooccurring interactions:
                    multiplicity_series[col.replace("b_", "")] = count_nonzero.mean()
                df.loc[feat_names_target, target] = multiplicity_series
            elif metric == "proportion":
                # Compute total number of target-expressing cells, and the indices of target-expressing cells:
                target_expr_cells_indices = np.where(adata[:, target].X.toarray() != 0)[0]
                # Compute total number of target-expressing cells:
                n_target_expr_cells = len(target_expr_cells_indices)
                # Extract only the rows of coef that correspond to target-expressing cells:
                coef_target_expr = coef.iloc[target_expr_cells_indices, :]
                # Compute number of cells for which each interaction is inferred to be present from among the
                # target-expressing cells:
                n_nonzero_interactions = np.sum(coef_target_expr != 0, axis=0)
                proportions = n_nonzero_interactions / n_target_expr_cells
                proportions.index = [idx.replace("b_", "") for idx in proportions.index]
                # Compute proportion:
                df.loc[feat_names_target, target] = proportions
            elif metric == "specificity":
                # Design matrix will be used for this mode:
                dm = self.design_matrix[feat_names_target]

                # Compute total number of target-expressing cells, and the indices of target-expressing cells:
                target_expr_cells_indices = np.where(adata[:, target].X.toarray() != 0)[0]
                # Intersection of each interaction w/ target-expressing cells to determine the numerator for the
                # proportion:
                intersections = pd.Series(0, index=columns)
                for col in columns:
                    nz_indices = np.where(coef[col].values != 0)[0]
                    intersection = np.intersect1d(target_expr_cells_indices, nz_indices)
                    intersections[col] = len(intersection)
                intersections.index = [idx.replace("b_", "") for idx in intersections.index]

                # Compute number of cells for which each interaction is inferred to be present to determine the
                # denominator for the proportion:
                n_nonzero_interactions = np.sum(dm != 0, axis=0)
                proportions = intersections / n_nonzero_interactions

                # Ratio of intersections to total number of nonzero values:
                df.loc[feat_names_target, target] = proportions
            elif metric == "mean":
                means = np.mean(coef, axis=0)
                means.index = [idx.replace("b_", "") for idx in means.index]
                df.loc[:, target] = means
            elif metric == "fc":
                # log1p transform AnnData to mitigate the effect of large numerical outliers:
                adata.X = log1p(adata)

                # Get indices of zero effect and predicted nonzero effect:
                for col in columns:
                    feat = col.replace("b_", "")
                    nz_effect_indices = np.where(coef[col].values != 0)[0]
                    if len(nz_effect_indices) < 100:
                        self.logger.info(f"Interaction {feat} has too few nonzero effects. Skipping...")
                        df.loc[feat, target] = 0.0
                        df_pvals.loc[feat, target] = 1.0
                        continue
                    zero_effect_indices = np.where(coef[col].values == 0)[0]

                    # Compute mean target expression for both subsets:
                    mean_target_nonzero = adata[nz_effect_indices, target].X.mean()
                    mean_target_zero = adata[zero_effect_indices, target].X.mean()

                    # Compute fold-change:
                    df.loc[feat, target] = mean_target_nonzero / mean_target_zero
                    # Compute p-value:
                    _, pval = scipy.stats.ranksums(
                        adata[nz_effect_indices, target].X.toarray(), adata[zero_effect_indices, target].X.toarray()
                    )
                    df_pvals.loc[feat, target] = pval

        # For metric = fold change, significance of the fold-change:
        if metric == "fc":
            # Multiple testing correction for each target using the Benjamin-Hochberg method:
            for col in df_pvals.columns:
                df_pvals[col] = multitesting_correction(df_pvals[col], method="fdr_bh")

            # Optionally, for plotting, retain fold-changes w/ significant corrected p-values:
            if plot_significant:
                df[df_pvals > 0.05] = 0.0

        # Formatting for visualization:
        df.index = [replace_col_with_collagens(idx) for idx in df.index]
        if metric == "fc" or metric == "fc_qvals":
            df_pvals.index = [replace_col_with_collagens(idx) for idx in df_pvals.index]
        df.index = [replace_hla_with_hlas(idx) for idx in df.index]
        if metric == "fc" or metric == "fc_qvals":
            df_pvals.index = [replace_hla_with_hlas(idx) for idx in df_pvals.index]

        # Plot preprocessing:
        # For metric = fold change q-values, compute the log-transformed fold change:
        if metric == "fc_qvals":
            df_log = np.log10(df_pvals.values)
            df_log[df_log < cut_pvals] = cut_pvals
            df_pvals = pd.DataFrame(df_log, index=df_pvals.index, columns=df_pvals.columns)
            if center is None:
                center = np.percentile(df_pvals.values.flatten(), 30)
            else:
                center = np.percentile(df_pvals.values.flatten(), center)
            # Adjust cmap such that the value typically corresponding to the minimum is the max- the max p-value is
            # the least significant:
            cmap = f"{cmap}_r"
            label = "$\log_{10}$ FDR-corrected pvalues"
            title = "Significance of target gene expression \n fold-change for each interaction"
        elif metric == "fc":
            # Set values below cutoff to 1 (no fold-change):
            df[(df < metric_threshold + 1) & (df > 1 - metric_threshold)] = 1.0
            if center is None:
                center = np.percentile(df.values.flatten(), 30)
            else:
                center = np.percentile(df.values.flatten(), center)
            vmax = np.max(np.abs(df.values))
            vmin = 0.0
            label = "Fold-change, $\log_{e}$ expression"
            title = "Target gene expression \n fold-change for each interaction"
        elif metric == "mean":
            df[np.abs(df) < metric_threshold] = 0.0
            if center is None:
                center = np.percentile(df.values.flatten(), 30)
            else:
                center = np.percentile(df.values.flatten(), center)
            vmax = np.max(df.values)
            vmin = np.min(df.values)
            label = "Mean effect size"
            title = "Mean effect size for \n each interaction on each target"
        elif metric in ["number", "multiplicity", "proportion", "specificity"]:
            if normalize:
                df = (df - df.min()) / (df.max() - df.min() + 1e-8)
                if center is None:
                    center = 0.5
            else:
                if metric == "number" or metric == "multiplicity":
                    if center is None:
                        center = np.percentile(df.values.flatten(), 30)
                    else:
                        center = np.percentile(df.values.flatten(), center)
                elif metric == "proportion" or metric == "specificity":
                    if center is None:
                        center = 0.3
            df[df < metric_threshold] = 0.0
            vmax = np.max(np.abs(df.values))
            vmin = 0
            if metric == "number":
                label = "Number of cells" if not normalize else "Number of cells (normalized)"
                title = (
                    "Number of cells w/ predicted effect \n on target for each interaction"
                    if not normalize
                    else "Number of cells w/ predicted effect \n on target for each interaction (normalized)"
                )
            elif metric == "proportion":
                label = "Proportion of cells" if not normalize else "Proportion of cells (normalized)"
                title = (
                    "Proportion of cells w/ predicted effect \n on target for each interaction"
                    if not normalize
                    else "Proportion of cells w/ predicted effect \n on target for each interaction (normalized)"
                )
            elif metric == "specificity":
                label = "Specificity" if not normalize else "Specificity (normalized)"
                title = (
                    "Exclusivity of predicted effect \n on target for each interaction"
                    if not normalize
                    else "Proportion of cells w/ predicted effect \n on target for each interaction (normalized)"
                )

        # Plot heatmap:
        fig, ax = plt.subplots(nrows=1, ncols=1, figsize=figsize)
        if metric == "fc_qvals":
            qv = sns.heatmap(
                df_pvals,
                square=True,
                linecolor="grey",
                linewidths=0.3,
                cbar_kws={"label": label, "location": "top"},
                cmap=cmap,
                center=center,
                vmin=cut_pvals,
                vmax=0,
                ax=ax,
            )

            # Outer frame:
            for _, spine in qv.spines.items():
                spine.set_visible(True)
                spine.set_linewidth(0.75)
        else:
            mask = df == 0
            m = sns.heatmap(
                df,
                square=True,
                linecolor="grey",
                linewidths=0.3,
                cbar_kws={"label": label, "location": "top"},
                cmap=cmap,
                center=center,
                vmin=vmin,
                vmax=vmax,
                mask=mask,
                ax=ax,
            )

            # Outer frame:
            for _, spine in m.spines.items():
                spine.set_visible(True)
                spine.set_linewidth(0.75)

        # Adjust colorbar label font size
        cbar = m.collections[0].colorbar
        cbar.set_label(label, fontsize=fontsize * 1.1)
        # Adjust colorbar tick font size
        cbar.ax.tick_params(labelsize=fontsize)

        plt.xlabel("Target gene", fontsize=fontsize * 1.1)
        plt.ylabel("Interaction", fontsize=fontsize * 1.1)
        plt.title(title, fontsize=fontsize * 1.25)
        plt.tight_layout()

        # Use the saved name for the AnnData object to define part of the name of the saved file:
        base_name = os.path.basename(self.adata_path)
        adata_id = os.path.splitext(base_name)[0]
        prefix = f"{adata_id}_{metric}"
        # Save figure:
        save_return_show_fig_utils(
            save_show_or_return=save_show_or_return,
            show_legend=False,
            background="white",
            prefix=prefix,
            save_kwargs=save_kwargs,
            total_panels=1,
            fig=fig,
            axes=ax,
            return_all=False,
            return_all_list=None,
        )

        if save_df:
            if metric == "fc_qvals":
                df_pvals.to_csv(os.path.join(output_folder, f"{prefix}.csv"))
            else:
                df.to_csv(os.path.join(output_folder, f"{prefix}.csv"))

    def moran_i_signaling_effects(
        self,
        targets: Optional[Union[str, List[str]]] = None,
        k: int = 10,
        weighted: Literal["kernel", "knn"] = "knn",
        permutations: int = 1000,
        n_jobs: int = 1,
    ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Computes spatial enrichment of signaling effects.

        Args:
            targets: Can optionally specify a subset of the targets to compute this on. If not given, will use all
                targets that were specified in model fitting.
            k: Number of k-nearest neighbors to use for Moran's I computation
            weighted: Whether to use a kernel-weighted or k-nearest neighbors approach to calculate spatial weights
            permutations: Number of random permutations for calculation of pseudo-p_values.
            n_jobs: Number of jobs to use for parallelization. If -1, all available CPUs are used. If 1 is given,
                no parallel computing code is used at all.

        Returns:
            signaling_moran_df: DataFrame with Moran's I scores for each target.
            signaling_moran_pvals: DataFrame with p-values for each Moran's I score.
        """
        if weighted != "kernel" and weighted != "knn":
            raise ValueError("Invalid argument given to 'weighted' parameter. Must be 'kernel' or 'knn'.")

        # Check inputs:
        if targets is not None:
            if isinstance(targets, str):
                targets = [targets]
            elif not isinstance(targets, list):
                raise ValueError(f"targets must be a list or string, not {type(targets)}.")

        # Get Moran's I scores for each target:
        feat_names = [feat for feat in self.feature_names if feat != "intercept"]
        signaling_moran_df = pd.DataFrame(0, index=feat_names, columns=targets)
        signaling_moran_pvals = pd.DataFrame(1, index=feat_names, columns=targets)
        coords = self.coords
        if weighted == "kernel":
            kw = lib.weights.Kernel(coords, k, function="gaussian")
            W = lib.weights.W(kw.neighbors, kw.weights)
        else:
            kd = lib.cg.KDTree(coords)
            nw = lib.weights.KNN(kd, k)
            W = lib.weights.W(nw.neighbors, nw.weights)

        # Moran I for a single interaction:
        def _single(interaction, X_df, W, permutations):
            cur_X = X_df[interaction].values
            mbi = explore.esda.moran.Moran(cur_X, W, permutations=permutations, two_tailed=False)
            Moran_I = mbi.I
            p_value = mbi.p_sim
            statistics = mbi.z_sim
            return [Moran_I, p_value, statistics]

        for target in targets:
            # Get coefficients for this key
            coef = self.coeffs[target]
            effects = coef[[col for col in coef.columns if col.startswith("b_") and "intercept" not in col]]
            effects.columns = [col.split("_")[1] for col in effects.columns]

            # Parallel computation of Moran's I for all interactions for this target:
            res = Parallel(n_jobs)(delayed(_single)(interaction, effects, W, permutations) for interaction in effects)
            res = pd.DataFrame(res, columns=["moran_i", "moran_p_val", "moran_z"], index=effects.columns)
            res["moran_q_val"] = multitesting_correction(res["moran_p_val"], method="fdr_bh")
            signaling_moran_df.loc[effects.columns, target] = res["moran_i"]
            signaling_moran_pvals.loc[effects.columns, target] = res["moran_q_val"]

        return signaling_moran_df, signaling_moran_pvals

    def visualize_combinatorial_effects(self):
        """For future work!"""

    def get_effect_potential(
        self,
        target: Optional[str] = None,
        ligand: Optional[str] = None,
        receptor: Optional[str] = None,
        sender_cell_type: Optional[str] = None,
        receiver_cell_type: Optional[str] = None,
        spatial_weights_membrane_bound: Optional[Union[np.ndarray, scipy.sparse.spmatrix]] = None,
        spatial_weights_secreted: Optional[Union[np.ndarray, scipy.sparse.spmatrix]] = None,
        spatial_weights_niche: Optional[Union[np.ndarray, scipy.sparse.spmatrix]] = None,
        store_summed_potential: bool = True,
    ) -> Tuple[scipy.sparse.spmatrix, np.ndarray, np.ndarray]:
        """For each cell, computes the 'signaling effect potential', interpreted as a quantification of the strength of
        effect of intercellular communication on downstream expression in a given cell mediated by any given other cell
        with any combination of ligands and/or cognate receptors, as inferred from the model results. Computations are
        similar to those of :func ~`.inferred_effect_direction`, but stops short of computing vector fields.

        Args:
            target: Optional string to select target from among the genes used to fit the model to compute signaling
                effects for. Note that this function takes only one target at a time. If not given, will take the
                first name from among all targets.
            ligand: Needed if :attr `mod_type` is 'ligand'; select ligand from among the ligands used to fit the
                model to compute signaling potential.
            receptor: Needed if :attr `mod_type` is 'lr'; together with 'ligand', used to select ligand-receptor pair
                from among the ligand-receptor pairs used to fit the model to compute signaling potential.
            sender_cell_type: Can optionally be used to select cell type from among the cell types used to fit the model
                to compute sent potential. Must be given if :attr `mod_type` is 'niche'.
            receiver_cell_type: Can optionally be used to condition sent potential on receiver cell type.

            store_summed_potential: If True, will store both sent and received signaling potential as entries in
                .obs of the AnnData object.

        Returns:
            effect_potential: Sparse array of shape [n_samples, n_samples]; proxy for the "signaling effect potential"
                with respect to a particular target gene between each sender-receiver pair of cells.
            normalized_effect_potential_sum_sender: Array of shape [n_samples,]; for each sending cell, the sum of the
                signaling potential to all receiver cells for a given target gene, normalized between 0 and 1.
            normalized_effect_potential_sum_receiver: Array of shape [n_samples,]; for each receiving cell, the sum of
                the signaling potential from all sender cells for a given target gene, normalized between 0 and 1.
        """

        if self.mod_type == "receptor":
            raise ValueError("Sent potential is not defined for receptor models.")

        if target is None:
            if self.target_for_downstream is not None:
                target = self.target_for_downstream
            else:
                self.logger.info(
                    "Target gene not provided for :func `get_effect_potential`. Using first target " "listed."
                )
                target = list(self.coeffs.keys())[0]

        # Check for valid inputs:
        if ligand is None:
            if self.ligand_for_downstream is not None:
                ligand = self.ligand_for_downstream
            else:
                if self.mod_type == "ligand" or self.mod_type == "lr":
                    raise ValueError("Must provide ligand for ligand models.")

        if receptor is None:
            if self.receptor_for_downstream is not None:
                receptor = self.receptor_for_downstream
            else:
                if self.mod_type == "lr":
                    raise ValueError("Must provide receptor for lr models.")

        if sender_cell_type is None:
            if self.sender_ct_for_downstream is not None:
                sender_cell_type = self.sender_ct_for_downstream
            else:
                if self.mod_type == "niche":
                    raise ValueError("Must provide sender cell type for niche models.")

        if receiver_cell_type is None and self.receiver_ct_for_downstream is not None:
            receiver_cell_type = self.receiver_ct_for_downstream

        if spatial_weights_membrane_bound is None:
            # Try to load spatial weights, else re-compute them:
            membrane_bound_path = os.path.join(
                os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_membrane_bound.npz"
            )
            try:
                spatial_weights_membrane_bound = scipy.sparse.load_npz(membrane_bound_path)
            except:
                # Get spatial weights given bandwidth value- each row corresponds to a sender, each column to a receiver.
                # Note: this is the same process used in model setup.
                spatial_weights_membrane_bound = self._compute_all_wi(
                    self.n_neighbors_membrane_bound, bw_fixed=False, exclude_self=True, verbose=False
                )
        if spatial_weights_secreted is None:
            secreted_path = os.path.join(
                os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_secreted.npz"
            )
            try:
                spatial_weights_secreted = scipy.sparse.load_npz(secreted_path)
            except:
                spatial_weights_secreted = self._compute_all_wi(
                    self.n_neighbors_secreted, bw_fixed=False, exclude_self=True, verbose=False
                )

        # Testing: compare both ways:
        coeffs = self.coeffs[target]
        # Set negligible coefficients to zero:
        coeffs[coeffs.abs() < 1e-2] = 0

        # Target indicator array:
        target_expr = self.targets_expr[target].values.reshape(1, -1)
        target_indicator = np.where(target_expr != 0, 1, 0)

        # For ligand models, "signaling potential" can only use the ligand information. For lr models, it can further
        # conditioned on the receptor expression:
        if self.mod_type == "ligand" or self.mod_type == "lr":
            if self.mod_type == "ligand" and ligand is None:
                raise ValueError("Must provide ligand name for ligand model.")
            elif self.mod_type == "lr" and (ligand is None or receptor is None):
                raise ValueError("Must provide both ligand name and receptor name for lr model.")

            if self.mod_type == "lr":
                if "/" in ligand:
                    multi_interaction = True
                    lr_pair = f"{ligand}:{receptor}"
                else:
                    multi_interaction = False
                    lr_pair = (ligand, receptor)
                if lr_pair not in self.lr_pairs and lr_pair not in self.feature_names:
                    raise ValueError(
                        "Invalid ligand-receptor pair given. Check that input to 'lr_pair' is given in "
                        "the form of a tuple."
                    )
            else:
                multi_interaction = False

            # Check if ligand is membrane-bound or secreted:
            matching_rows = self.lr_db[self.lr_db["from"].isin(ligand.split("/"))]
            if (
                matching_rows["type"].str.contains("Secreted Signaling").any()
                or matching_rows["type"].str.contains("ECM-Receptor").any()
            ):
                spatial_weights = spatial_weights_secreted
            else:
                spatial_weights = spatial_weights_membrane_bound

            # Use the non-lagged ligand expression to construct ligand indicator array:
            if not multi_interaction:
                ligand_expr = self.ligands_expr_nonlag[ligand].values.reshape(-1, 1)
            else:
                all_multi_ligands = ligand.split("/")
                ligand_expr = self.ligands_expr_nonlag[all_multi_ligands].mean(axis=1).values.reshape(-1, 1)

            # Referred to as "sent potential"
            sent_potential = spatial_weights.multiply(ligand_expr)
            sent_potential.eliminate_zeros()

            # If "lr", incorporate the receptor expression indicator array:
            if self.mod_type == "lr":
                receptor_expr = self.receptors_expr[receptor].values.reshape(1, -1)
                sent_potential = sent_potential.multiply(receptor_expr)
                sent_potential.eliminate_zeros()

            # Find the location of the correct coefficient:
            if self.mod_type == "ligand":
                ligand_coeff_label = f"b_{ligand}"
                idx = coeffs.columns.get_loc(ligand_coeff_label)
            elif self.mod_type == "lr":
                lr_coeff_label = f"b_{ligand}:{receptor}"
                idx = coeffs.columns.get_loc(lr_coeff_label)

            coeff = coeffs.iloc[:, idx].values.reshape(1, -1)
            effect_sign = np.where(coeff > 0, 1, -1)
            # Weight each column by the coefficient magnitude and finally by the indicator for expression/no
            # expression of the target and store as sparse array:
            sig_interm = sent_potential.multiply(coeff)
            sig_interm.eliminate_zeros()
            effect_potential = sig_interm.multiply(target_indicator)
            effect_potential.eliminate_zeros()

        elif self.mod_type == "niche":
            if sender_cell_type is None:
                raise ValueError("Must provide sending cell type name for niche models.")

            if spatial_weights_niche is None:
                niche_weights_path = os.path.join(
                    os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_niche.npz"
                )
                try:
                    spatial_weights_niche = scipy.sparse.load_npz(niche_weights_path)
                except:
                    spatial_weights_niche = self._compute_all_wi(
                        self.n_neighbors_secreted, bw_fixed=False, exclude_self=True, verbose=False
                    )

            sender_cell_type = self.cell_categories[sender_cell_type].values.reshape(-1, 1)
            # Get sending cells only of the specified type:
            sent_potential = spatial_weights_niche.multiply(sender_cell_type)
            sent_potential.eliminate_zeros()

            # Check whether to condition on receiver cell type:
            if receiver_cell_type is not None:
                receiver_cell_type = self.cell_categories[receiver_cell_type].values.reshape(1, -1)
                sent_potential = sent_potential.multiply(receiver_cell_type)
                sent_potential.eliminate_zeros()

            sending_ct_coeff_label = f"b_Proxim{sender_cell_type}"
            coeff = coeffs[sending_ct_coeff_label].values.reshape(1, -1)
            effect_sign = np.where(coeff > 0, 1, -1)
            # Weight each column by the coefficient magnitude and finally by the indicator for expression/no expression
            # of the target and store as sparse array:
            sig_interm = sent_potential.multiply(coeff)
            sig_interm.eliminate_zeros()
            effect_potential = sig_interm.multiply(target_indicator)
            effect_potential.eliminate_zeros()

        effect_potential_sum_sender = np.array(effect_potential.sum(axis=1)).reshape(-1)
        sign = np.where(effect_potential_sum_sender > 0, 1, -1)
        # Take the absolute value to get the overall measure of the effect- after normalizing, add the sign back in:
        effect_potential_sum_sender = np.abs(effect_potential_sum_sender)
        normalized_effect_potential_sum_sender = (effect_potential_sum_sender - np.min(effect_potential_sum_sender)) / (
            np.max(effect_potential_sum_sender) - np.min(effect_potential_sum_sender)
        )
        normalized_effect_potential_sum_sender = normalized_effect_potential_sum_sender * sign

        effect_potential_sum_receiver = np.array(effect_potential.sum(axis=0)).reshape(-1)
        sign = np.where(effect_potential_sum_receiver > 0, 1, -1)
        # Take the absolute value to get the overall measure of the effect- after normalizing, add the sign back in:
        effect_potential_sum_receiver = np.abs(effect_potential_sum_receiver)
        normalized_effect_potential_sum_receiver = (
            effect_potential_sum_receiver - np.min(effect_potential_sum_receiver)
        ) / (np.max(effect_potential_sum_receiver) - np.min(effect_potential_sum_receiver))
        normalized_effect_potential_sum_receiver = normalized_effect_potential_sum_receiver * sign

        # Store summed sent/received potential:
        if store_summed_potential:
            if self.mod_type == "niche":
                if receiver_cell_type is None:
                    self.adata.obs[
                        f"norm_sum_sent_effect_potential_{sender_cell_type}_for_{target}"
                    ] = normalized_effect_potential_sum_sender

                    self.adata.obs[
                        f"norm_sum_received_effect_potential_from_{sender_cell_type}_for_{target}"
                    ] = normalized_effect_potential_sum_receiver
                else:
                    self.adata.obs[
                        f"norm_sum_sent_{sender_cell_type}_effect_potential_to_{receiver_cell_type}_for_{target}"
                    ] = normalized_effect_potential_sum_sender

                    self.adata.obs[
                        f"norm_sum_{receiver_cell_type}_received_effect_potential_from_{sender_cell_type}_for_{target}"
                    ] = normalized_effect_potential_sum_receiver

            elif self.mod_type == "ligand":
                if "/" in ligand:
                    ligand = replace_col_with_collagens(ligand)
                    ligand = replace_hla_with_hlas(ligand)

                self.adata.obs[
                    f"norm_sum_sent_effect_potential_{ligand}_for_{target}"
                ] = normalized_effect_potential_sum_sender

                self.adata.obs[
                    f"norm_sum_received_effect_potential_from_{ligand}_for_{target}"
                ] = normalized_effect_potential_sum_receiver

            elif self.mod_type == "lr":
                if "/" in ligand:
                    ligand = replace_col_with_collagens(ligand)
                    ligand = replace_hla_with_hlas(ligand)

                self.adata.obs[
                    f"norm_sum_sent_effect_potential_{ligand}_for_{target}_via_{receptor}"
                ] = normalized_effect_potential_sum_sender

                self.adata.obs[
                    f"norm_sum_received_effect_potential_from_{ligand}_for_{target}_via_{receptor}"
                ] = normalized_effect_potential_sum_receiver

            self.adata.obs["effect_sign"] = effect_sign.reshape(-1, 1)

        return effect_potential, normalized_effect_potential_sum_sender, normalized_effect_potential_sum_receiver

    def get_pathway_potential(
        self,
        pathway: Optional[str] = None,
        target: Optional[str] = None,
        spatial_weights_secreted: Optional[Union[np.ndarray, scipy.sparse.spmatrix]] = None,
        spatial_weights_membrane_bound: Optional[Union[np.ndarray, scipy.sparse.spmatrix]] = None,
        store_summed_potential: bool = True,
    ):
        """For each cell, computes the 'pathway effect potential', which is an aggregation of the effect potentials
        of all pathway member ligand-receptor pairs (or all pathway member ligands, for ligand-only models).

        Args:
            pathway: Name of pathway to compute pathway effect potential for.
            target: Optional string to select target from among the genes used to fit the model to compute signaling
                effects for. Note that this function takes only one target at a time. If not given, will take the
                first name from among all targets.
            spatial_weights_secreted: Optional pairwise spatial weights matrix for secreted factors
            spatial_weights_membrane_bound: Optional pairwise spatial weights matrix for membrane-bound factors
            store_summed_potential: If True, will store both sent and received signaling potential as entries in
                .obs of the AnnData object.

        Returns:
            pathway_sum_potential: Array of shape [n_samples, n_samples]; proxy for the combined "signaling effect
                potential" with respect to a particular target gene for ligand-receptor pairs in a pathway.
            normalized_pathway_effect_potential_sum_sender: Array of shape [n_samples,]; for each sending cell,
                the sum of the pathway sum potential to all receiver cells for a given target gene, normalized between
                0 and 1.
            normalized_pathway_effect_potential_sum_receiver: Array of shape [n_samples,]; for each receiving cell,
                the sum of the pathway sum potential from all sender cells for a given target gene, normalized between
                0 and 1.
        """

        if self.mod_type not in ["lr", "ligand"]:
            raise ValueError("Cannot compute pathway effect potential, since fitted model does not use ligands.")

        # Columns consist of the spatial weights of each observation- convolve with expression of each ligand to
        # get proxy of ligand signal "sent", weight by the local coefficient value to get a proxy of the "signal
        # functionally received" in generating the downstream effect and store in .obsp.
        if target is None and self.target_for_downstream is not None:
            target = self.target_for_downstream
        else:
            self.logger.info("Target gene not provided for :func `get_effect_potential`. Using first target listed.")
            target = list(self.coeffs.keys())[0]

        if pathway is None and self.pathway_for_downstream is not None:
            pathway = self.pathway_for_downstream
        else:
            raise ValueError("Must provide pathway to analyze.")

        lr_db_subset = self.lr_db[self.lr_db["pathway"] == pathway]
        all_receivers = list(set(lr_db_subset["to"]))
        all_senders = list(set(lr_db_subset["from"]))

        if self.mod_type == "lr":
            self.logger.info(
                "Computing pathway effect potential for ligand-receptor pairs in pathway, since :attr "
                "`mod_type` is 'lr'."
            )

            # Get ligand-receptor combinations in the pathway from our model:
            valid_lr_combos = []
            for col in self.design_matrix.columns:
                parts = col.split(":")
                if parts[1] in all_receivers:
                    valid_lr_combos.append((parts[0], parts[1]))

            if len(valid_lr_combos) < 3:
                raise ValueError(
                    f"Pathway effect potential computation for pathway {pathway} is unsuitable for this model, "
                    f"since there are fewer than three valid ligand-receptor pairs in the pathway that were "
                    f"incorporated in the initial model."
                )

            all_pathway_member_effects = {}
            for j, col in enumerate(valid_lr_combos):
                ligand = col[0]
                receptor = col[1]
                effect_potential, _, _ = self.get_effect_potential(
                    target=target,
                    ligand=ligand,
                    receptor=receptor,
                    spatial_weights_secreted=spatial_weights_secreted,
                    spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                    store_summed_potential=False,
                )
                all_pathway_member_effects[f"effect_potential_{ligand}_{receptor}_on_{target}"] = effect_potential

        elif self.mod_type == "ligand":
            self.logger.info(
                "Computing pathway effect potential for ligands in pathway, since :attr `mod_type` is " "'ligand'."
            )

            all_pathway_member_effects = {}
            for j, col in enumerate(all_senders):
                ligand = col
                effect_potential, _, _ = self.get_effect_potential(
                    target=target,
                    ligand=ligand,
                    spatial_weights_secreted=spatial_weights_secreted,
                    spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                    store_summed_potential=False,
                )
                all_pathway_member_effects[f"effect_potential_{ligand}_on_{target}"] = effect_potential

        # Combine results for all ligand-receptor pairs in the pathway:
        pathway_sum_potential = None
        for key in all_pathway_member_effects.keys():
            if pathway_sum_potential is None:
                pathway_sum_potential = all_pathway_member_effects[key]
            else:
                pathway_sum_potential += all_pathway_member_effects[key]
        # self.adata.obsp[f"effect_potential_{pathway}_on_{target}"] = pathway_sum_potential

        pathway_effect_potential_sum_sender = np.array(pathway_sum_potential.sum(axis=1)).reshape(-1)
        normalized_pathway_effect_potential_sum_sender = (
            pathway_effect_potential_sum_sender - np.min(pathway_effect_potential_sum_sender)
        ) / (np.max(pathway_effect_potential_sum_sender) - np.min(pathway_effect_potential_sum_sender))

        pathway_effect_potential_sum_receiver = np.array(pathway_sum_potential.sum(axis=0)).reshape(-1)
        normalized_effect_potential_sum_receiver = (
            pathway_effect_potential_sum_receiver - np.min(pathway_effect_potential_sum_receiver)
        ) / (np.max(pathway_effect_potential_sum_receiver) - np.min(pathway_effect_potential_sum_receiver))

        if store_summed_potential:
            if self.mod_type == "lr":
                send_key = f"norm_sum_sent_effect_potential_{pathway}_lr_for_{target}"
                receive_key = f"norm_sum_received_effect_potential_{pathway}_lr_for_{target}"
            elif self.mod_type == "ligand":
                send_key = f"norm_sum_sent_effect_potential_{pathway}_ligands_for_{target}"
                receive_key = f"norm_sum_received_effect_potential_{pathway}_ligands_for_{target}"

            self.adata.obs[send_key] = normalized_pathway_effect_potential_sum_sender
            self.adata.obs[receive_key] = normalized_effect_potential_sum_receiver

        return (
            pathway_sum_potential,
            normalized_pathway_effect_potential_sum_sender,
            normalized_effect_potential_sum_receiver,
        )

    def inferred_effect_direction(
        self,
        targets: Optional[Union[str, List[str]]] = None,
        compute_pathway_effect: bool = False,
    ):
        """For visualization purposes, used for models that consider ligand expression (:attr `mod_type` is 'ligand' or
        'lr' (for receptor models, assigning directionality is impossible and for niche models, it makes much less
        sense to draw/compute a vector field). Construct spatial vector fields to infer the directionality of
        observed effects (the "sources" of the downstream expression).

        Parts of this function are inspired by 'communication_direction' from COMMOT: https://github.com/zcang/COMMOT

        Args:
            targets: Optional string or list of strings to select targets from among the genes used to fit the model
                to compute signaling effects for. If not given, will use all targets.
            compute_pathway_effect: Whether to compute the effect potential for each pathway in the model. If True,
                will collectively take the effect potential of all pathway components. If False, will compute effect
                potential for each for each individual signal.
        """
        if not self.mod_type == "ligand" or self.mod_type == "lr":
            raise ValueError(
                "Direction of effect can only be inferred if ligand expression is used as part of the " "model."
            )

        if self.compute_pathway_effect is not None:
            compute_pathway_effect = self.compute_pathway_effect
        if self.target_for_downstream is not None:
            targets = self.target_for_downstream

        # Get spatial weights given bandwidth value- each row corresponds to a sender, each column to a receiver:
        # Try to load spatial weights for membrane-bound and secreted ligands, compute if not found:
        membrane_bound_path = os.path.join(
            os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_membrane_bound.npz"
        )
        secreted_path = os.path.join(
            os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_secreted.npz"
        )

        try:
            spatial_weights_membrane_bound = scipy.sparse.load_npz(membrane_bound_path)
            spatial_weights_secreted = scipy.sparse.load_npz(secreted_path)
        except:
            bw_mb = (
                self.n_neighbors_membrane_bound
                if self.distance_membrane_bound is None
                else self.distance_membrane_bound
            )
            bw_fixed = True if self.distance_membrane_bound is not None else False
            spatial_weights_membrane_bound = self._compute_all_wi(
                bw=bw_mb,
                bw_fixed=bw_fixed,
                exclude_self=True,
                verbose=False,
            )
            self.logger.info(f"Saving spatial weights for membrane-bound ligands to {membrane_bound_path}.")
            scipy.sparse.save_npz(membrane_bound_path, spatial_weights_membrane_bound)

            bw_s = self.n_neighbors_membrane_bound if self.distance_secreted is None else self.distance_secreted
            bw_fixed = True if self.distance_secreted is not None else False
            # Autocrine signaling is much easier with secreted signals:
            spatial_weights_secreted = self._compute_all_wi(
                bw=bw_s,
                bw_fixed=bw_fixed,
                exclude_self=False,
                verbose=False,
            )
            self.logger.info(f"Saving spatial weights for secreted ligands to {secreted_path}.")
            scipy.sparse.save_npz(secreted_path, spatial_weights_secreted)

        # Columns consist of the spatial weights of each observation- convolve with expression of each ligand to
        # get proxy of ligand signal "sent", weight by the local coefficient value to get a proxy of the "signal
        # functionally received" in generating the downstream effect and store in .obsp.
        if targets is None:
            targets = self.coeffs.keys()
        elif isinstance(targets, str):
            targets = [targets]

        if self.filter_targets:
            pearson_dict = {}
            for target in targets:
                observed = self.adata[:, target].X.toarray().reshape(-1, 1)
                predicted = self.predictions[target].reshape(-1, 1)

                rp, _ = pearsonr(observed, predicted)
                pearson_dict[target] = rp

            targets = [target for target in targets if pearson_dict[target] > self.filter_target_threshold]

        queries = self.lr_pairs if self.mod_type == "lr" else self.ligands

        if compute_pathway_effect:
            # Find pathways that are represented among the ligands or ligand-receptor pairs:
            pathways = []
            for query in queries:
                if self.mod_type == "lr":
                    ligand = query.split(":")[0]
                    receptor = query.split(":")[1]
                    col_pathways = list(
                        set(
                            self.lr_db.loc[
                                (self.lr_db["from"] == ligand) & (self.lr_db["to"] == receptor), "pathway"
                            ].values
                        )
                    )
                    pathways.extend(col_pathways)
                elif self.mod_type == "ligand":
                    col_pathways = list(set(self.lr_db.loc[self.lr_db["from"] == query, "pathway"].values))
                    pathways.extend(col_pathways)
            # Before taking the set of pathways, count number of occurrences of each pathway in the list- remove
            # pathways for which there are fewer than three ligands or ligand-receptor pairs- these are not enough to
            # constitute a pathway:
            pathway_counts = Counter(pathways)
            pathways = [pathway for pathway, count in pathway_counts.items() if count >= 3]
            # Take the set of pathways:
            queries = list(set(pathways))

        for target in targets:
            for j, query in enumerate(queries):
                if self.mod_type == "lr":
                    if compute_pathway_effect:
                        (
                            effect_potential,
                            normalized_effect_potential_sum_sender,
                            normalized_effect_potential_sum_receiver,
                        ) = self.get_pathway_potential(
                            target=target,
                            pathway=query,
                            spatial_weights_secreted=spatial_weights_secreted,
                            spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        )
                    else:
                        ligand = query.split(":")[0]
                        receptor = query.split(":")[1]
                        (
                            effect_potential,
                            normalized_effect_potential_sum_sender,
                            normalized_effect_potential_sum_receiver,
                        ) = self.get_effect_potential(
                            target=target,
                            ligand=ligand,
                            receptor=receptor,
                            spatial_weights_secreted=spatial_weights_secreted,
                            spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        )
                else:
                    if compute_pathway_effect:
                        (
                            effect_potential,
                            normalized_effect_potential_sum_sender,
                            normalized_effect_potential_sum_receiver,
                        ) = self.get_pathway_potential(
                            target=target,
                            pathway=query,
                            spatial_weights_secreted=spatial_weights_secreted,
                            spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        )
                    else:
                        (
                            effect_potential,
                            normalized_effect_potential_sum_sender,
                            normalized_effect_potential_sum_receiver,
                        ) = self.get_effect_potential(
                            target=target,
                            ligand=query,
                            spatial_weights_secreted=spatial_weights_secreted,
                            spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        )

                # Compute vector field:
                self.define_effect_vf(
                    effect_potential,
                    normalized_effect_potential_sum_sender,
                    normalized_effect_potential_sum_receiver,
                    query,
                    target,
                )

        # Save AnnData object with effect direction information:
        adata_name = os.path.splitext(self.adata_path)[0]
        self.adata.write(f"{adata_name}_effect_directions.h5ad")

    def define_effect_vf(
        self,
        effect_potential: scipy.sparse.spmatrix,
        normalized_effect_potential_sum_sender: np.ndarray,
        normalized_effect_potential_sum_receiver: np.ndarray,
        sig: str,
        target: str,
        max_val: float = 0.05,
    ):
        """Given the pairwise effect potential array, computes the effect vector field.

        Args:
            effect_potential: Sparse array containing computed effect potentials- output from
                :func:`get_effect_potential`
            normalized_effect_potential_sum_sender: Array containing the sum of the effect potentials sent by each
                cell. Output from :func:`get_effect_potential`.
            normalized_effect_potential_sum_receiver: Array containing the sum of the effect potentials received by
                each cell. Output from :func:`get_effect_potential`.
            max_val: Constrains the size of the vector field vectors. Recommended to set within the order of
                magnitude of 1/100 of the desired plot dimensions.
            sig: Label for the mediating interaction (e.g. name of a ligand, name of a ligand-receptor pair, etc.)
            target: Name of the target that the vector field describes the effect for
        """
        sending_vf = np.zeros_like(self.coords)
        receiving_vf = np.zeros_like(self.coords)

        # Vector field for sent signal:
        effect_potential_lil = effect_potential.tolil()
        for i in range(self.n_samples):
            if len(effect_potential_lil.rows[i]) <= self.k:
                temp_idx = np.array(effect_potential_lil.rows[i], dtype=int)
                temp_val = np.array(effect_potential_lil.data[i], dtype=float)
            else:
                row_np = np.array(effect_potential_lil.rows[i], dtype=int)
                data_np = np.array(effect_potential_lil.data[i], dtype=float)
                temp_idx = row_np[np.argsort(-data_np)[: self.k]]
                temp_val = data_np[np.argsort(-data_np)[: self.k]]
            if len(temp_idx) == 0:
                continue
            elif len(temp_idx) == 1:
                avg_v = self.coords[temp_idx[0], :] - self.coords[i, :]
            else:
                temp_v = self.coords[temp_idx, :] - self.coords[i, :]
                temp_v = normalize(temp_v, norm="l2")
                avg_v = np.sum(temp_v * temp_val.reshape(-1, 1), axis=0)
            avg_v = normalize(avg_v.reshape(1, -1))
            sending_vf[i, :] = avg_v[0, :] * normalized_effect_potential_sum_sender[i]
        sending_vf = np.clip(sending_vf, -max_val, max_val)

        # Vector field for received signal:
        effect_potential_lil = effect_potential.T.tolil()
        for i in range(self.n_samples):
            if len(effect_potential_lil.rows[i]) <= self.k:
                temp_idx = np.array(effect_potential_lil.rows[i], dtype=int)
                temp_val = np.array(effect_potential_lil.data[i], dtype=float)
            else:
                row_np = np.array(effect_potential_lil.rows[i], dtype=int)
                data_np = np.array(effect_potential_lil.data[i], dtype=float)
                temp_idx = row_np[np.argsort(-data_np)[: self.k]]
                temp_val = data_np[np.argsort(-data_np)[: self.k]]
            if len(temp_idx) == 0:
                continue
            elif len(temp_idx) == 1:
                avg_v = self.coords[temp_idx, :] - self.coords[i, :]
            else:
                temp_v = self.coords[temp_idx, :] - self.coords[i, :]
                temp_v = normalize(temp_v, norm="l2")
                avg_v = np.sum(temp_v * temp_val.reshape(-1, 1), axis=0)
            avg_v = normalize(avg_v.reshape(1, -1))
            receiving_vf[i, :] = avg_v[0, :] * normalized_effect_potential_sum_receiver[i]
        receiving_vf = np.clip(receiving_vf, -max_val, max_val)

        del effect_potential

        # Shorten names if collagens/HLA in "sig":
        sig = replace_col_with_collagens(sig)
        sig = replace_hla_with_hlas(sig)

        self.adata.obsm[f"spatial_effect_sender_vf_{sig}_{target}"] = sending_vf
        self.adata.obsm[f"spatial_effect_receiver_vf_{sig}_{target}"] = receiving_vf

    # ---------------------------------------------------------------------------------------------------
    # Constructing gene regulatory networks
    # ---------------------------------------------------------------------------------------------------
    def CCI_deg_detection_setup(
        self,
        group_key: Optional[str] = None,
        sender_receiver_or_target_degs: Literal["sender", "receiver", "target"] = "sender",
        use_ligands: bool = True,
        use_receptors: bool = False,
        use_pathways: bool = False,
        use_targets: bool = False,
        use_cell_types: bool = False,
        compute_dim_reduction: bool = False,
    ):
        """Computes differential expression signatures of cells with various levels of ligand expression.

        Args:
            group_key: Key to add to .obs of the AnnData object created by this function, containing cell type labels
                for each cell. If not given, will use :attr `group_key`.
            sender_receiver_or_target_degs: Only makes a difference if 'use_pathways' or 'use_cell_types' is specified.
                Determines whether to compute DEGs for ligands, receptors or target genes. If 'use_pathways' is True,
                the value of this argument will determine whether ligands or receptors are used to define the model.
                Note that in either case, differential expression of TFs, binding factors, etc. will be computed in
                association w/ ligands/receptors/target genes (only valid if 'use_cell_types' and not 'use_pathways'
                is specified.
            use_ligands: Use ligand array for differential expression analysis. Will take precedent over
                sender/receiver cell type if also provided.
            use_receptors: Use receptor array for differential expression analysis. Will take precedent over
                sender/receiver cell type if also provided.
            use_pathways: Use pathway array for differential expression analysis. Will use ligands in these pathways
                to collectively compute signaling potential score. Will take precedent over sender cell types if
                also provided.
            use_targets: Use target array for differential expression analysis.
            use_cell_types: Use cell types to use for differential expression analysis. If given,
                will preprocess/construct the necessary components to initialize cell type-specific models. Note-
                should be used alongside 'use_ligands', 'use_receptors', 'use_pathways' or 'use_targets' to select
                which molecules to investigate in each cell type.
            compute_dim_reduction: Whether to compute PCA representation of the data subsetted to targets.
        """

        if group_key is None:
            group_key = self.group_key

        if (use_ligands and use_receptors) or (use_ligands and use_targets) or (use_receptors and use_targets):
            self.logger.info(
                "Multiple of 'use_ligands', 'use_receptors', 'use_targets' are given as function inputs. Note that "
                "'use_ligands' will take priority."
            )
        if sender_receiver_or_target_degs == "target" and use_pathways:
            raise ValueError("`sender_receiver_or_target_degs` cannot be 'target' if 'use_pathways' is True.")

        # Check if the array of additional molecules to query has already been created:
        output_dir = os.path.dirname(self.output_path)
        file_name = os.path.basename(self.adata_path).split(".")[0]
        if use_ligands:
            targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_ligands.txt")
        elif use_receptors:
            targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_receptors.txt")
        elif use_pathways:
            targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_pathways.txt")
        elif use_targets:
            targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_target_genes.txt")
        elif use_cell_types:
            targets_folder = os.path.join(output_dir, "cci_deg_detection")

        if not os.path.exists(os.path.join(output_dir, "cci_deg_detection")):
            os.makedirs(os.path.join(output_dir, "cci_deg_detection"))

        # Check for existing processed downstream-task AnnData object:
        if os.path.exists(os.path.join(output_dir, "cci_deg_detection", f"{file_name}.h5ad")):
            # Load files in case they are already existent:
            counts_plus = anndata.read_h5ad(os.path.join(output_dir, "cci_deg_detection", f"{file_name}.h5ad"))
            if use_ligands or use_pathways or use_receptors or use_targets:
                with open(targets_path, "r") as file:
                    targets = file.readlines()
            else:
                targets = pd.read_csv(targets_path, index_col=0)
            self.logger.info(
                "Found existing files for downstream analysis- skipping processing. Can proceed by running "
                ":func ~`self.CCI_sender_deg_detection()`."
            )
        # Else generate the necessary files:
        else:
            self.logger.info("Generating and saving AnnData object for downstream analysis...")
            if self.cci_dir is None:
                raise ValueError("Please provide :attr `cci_dir`.")

            if self.species == "human":
                grn = pd.read_csv(os.path.join(self.cci_dir, "human_GRN.csv"), index_col=0)
                rna_bp_db = pd.read_csv(os.path.join(self.cci_dir, "human_RBP_db.csv"), index_col=0)
                cof_db = pd.read_csv(os.path.join(self.cci_dir, "human_cofactors.csv"), index_col=0)
                tf_tf_db = pd.read_csv(os.path.join(self.cci_dir, "human_TF_TF_db.csv"), index_col=0)
                lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_human.csv"), index_col=0)
            elif self.species == "mouse":
                grn = pd.read_csv(os.path.join(self.cci_dir, "mouse_GRN.csv"), index_col=0)
                rna_bp_db = pd.read_csv(os.path.join(self.cci_dir, "mouse_RBP_db.csv"), index_col=0)
                cof_db = pd.read_csv(os.path.join(self.cci_dir, "mouse_cofactors.csv"), index_col=0)
                tf_tf_db = pd.read_csv(os.path.join(self.cci_dir, "mouse_TF_TF_db.csv"), index_col=0)
                lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_mouse.csv"), index_col=0)

            # Subset GRN and other databases to only include TFs that are in the adata object:
            grn = grn[[col for col in grn.columns if col in self.adata.var_names]]
            cof_db = cof_db[[col for col in cof_db.columns if col in self.adata.var_names]]
            tf_tf_db = tf_tf_db[[col for col in tf_tf_db.columns if col in self.adata.var_names]]

            analyze_pathway_ligands = sender_receiver_or_target_degs == "sender" and use_pathways
            analyze_pathway_receptors = sender_receiver_or_target_degs == "receiver" and use_pathways
            analyze_celltype_ligands = sender_receiver_or_target_degs == "sender" and use_cell_types
            analyze_celltype_receptors = sender_receiver_or_target_degs == "receiver" and use_cell_types
            analyze_celltype_targets = sender_receiver_or_target_degs == "target" and use_cell_types

            if use_ligands or analyze_pathway_ligands or analyze_celltype_ligands:
                database_ligands = list(set(lr_db["from"]))
                l_complexes = [elem for elem in database_ligands if "_" in elem]
                # Get individual components if any complexes are included in this list:
                ligand_set = [l for item in database_ligands for l in item.split("_")]
                ligand_set = [l for l in ligand_set if l in self.adata.var_names]
            elif use_receptors or analyze_pathway_receptors or analyze_celltype_receptors:
                database_receptors = list(set(lr_db["to"]))
                r_complexes = [elem for elem in database_receptors if "_" in elem]
                # Get individual components if any complexes are included in this list:
                receptor_set = [r for item in database_receptors for r in item.split("_")]
                receptor_set = [r for r in receptor_set if r in self.adata.var_names]
            elif use_targets or analyze_celltype_targets:
                target_set = self.targets_expr.columns

            signal = {}
            subsets = {}

            if use_ligands:
                if self.mod_type != "ligand" and self.mod_type != "lr":
                    raise ValueError(
                        "Sent signal from ligands cannot be used because the original specified 'mod_type' "
                        "does not use ligand expression."
                    )
                # Sent signal from ligand- use non-lagged version b/c the upstream factor effects on ligand expression
                # are an intrinsic property:
                # Some of the columns in the ligands dataframe may be complexes- identify the single genes that
                # compose these complexes:
                sig_df = self.ligands_expr_nonlag
                for col in sig_df.columns:
                    if col in l_complexes:
                        sig_df = sig_df.drop(col, axis=1)
                        for l in col.split("_"):
                            if scipy.sparse.issparse(self.adata.X):
                                gene_expr = self.adata[:, l].X.A
                            else:
                                gene_expr = self.adata[:, l].X
                            sig_df[l] = gene_expr

                signal["all"] = sig_df
                subsets["all"] = self.adata
            elif use_receptors:
                if self.mod_type != "receptor" and self.mod_type != "lr":
                    raise ValueError(
                        "Sent signal from receptors cannot be used because the original specified 'mod_type' "
                        "does not use receptor expression."
                    )
                # Received signal from receptor:
                # Some of the columns in the receptors dataframe may be complexes- identify the single genes that
                # compose these complexes:
                sig_df = self.receptors_expr
                for col in sig_df.columns:
                    if col in r_complexes:
                        sig_df = sig_df.drop(col, axis=1)
                        for r in col.split("_"):
                            if scipy.sparse.issparse(self.adata.X):
                                gene_expr = self.adata[:, r].X.A
                            else:
                                gene_expr = self.adata[:, r].X
                            sig_df[r] = gene_expr

                signal["all"] = sig_df
                subsets["all"] = self.adata
            elif use_pathways and sender_receiver_or_target_degs == "sender":
                if self.mod_type != "ligand" and self.mod_type != "lr":
                    raise ValueError(
                        "Sent signal from ligands cannot be used because the original specified 'mod_type' "
                        "does not use ligand expression."
                    )
                # Groupby pathways and take the arithmetic mean of the ligands/interactions in each relevant pathway:
                lig_to_pathway_map = lr_db.set_index("from")["pathway"].drop_duplicates().to_dict()
                mapped_ligands = self.ligands_expr_nonlag.copy()
                mapped_ligands.columns = self.ligands_expr_nonlag.columns.map(lig_to_pathway_map)
                signal["all"] = mapped_ligands.groupby(by=mapped_ligands.columns, axis=1).sum()
                subsets["all"] = self.adata
            elif use_pathways and sender_receiver_or_target_degs == "receiver":
                if self.mod_type != "receptor" and self.mod_type != "lr":
                    raise ValueError(
                        "Received signal from receptors cannot be used because the original specified 'mod_type' "
                        "does not use receptor expression."
                    )
                # Groupby pathways and take the arithmetic mean of the receptors/interactions in each relevant pathway:
                rec_to_pathway_map = lr_db.set_index("to")["pathway"].drop_duplicates().to_dict()
                mapped_receptors = self.receptors_expr.copy()
                mapped_receptors.columns = self.receptors_expr.columns.map(rec_to_pathway_map)
                signal["all"] = mapped_receptors.groupby(by=mapped_receptors.columns, axis=1).sum()
                subsets["all"] = self.adata
            elif use_targets:
                if self.targets_path is not None:
                    with open(self.targets_path, "r") as f:
                        targets = f.read().splitlines()
                else:
                    targets = self.custom_targets
                # Check that all targets can be found in the source AnnData object:
                targets = [t for t in targets if t in self.adata.var_names]
                targets_expr = pd.DataFrame(
                    self.adata[:, targets].X.A if scipy.sparse.issparse(self.adata.X) else self.adata[:, targets].X,
                    index=self.adata.obs_names,
                    columns=targets,
                )
                signal["all"] = targets_expr
                subsets["all"] = self.adata
            elif use_cell_types:
                if self.mod_type != "niche":
                    raise ValueError(
                        "Cell categories cannot be used because the original specified 'mod_type' does not "
                        "consider cell type. Change 'mod_type' to 'niche' if desired."
                    )

                # For downstream analysis through the lens of cell type, we can aid users in creating a downstream
                # effects model for each cell type:
                for cell_type in self.cell_categories.columns:
                    ct_subset = self.adata[self.adata.obs[self.group_key] == cell_type, :].copy()
                    subsets[cell_type] = ct_subset

                    if "ligand_set" in locals():
                        mols = ligand_set
                    elif "receptor_set" in locals():
                        mols = receptor_set
                    elif "target_set" in locals():
                        mols = target_set
                    ct_signaling = ct_subset[:, mols].copy()
                    # Find the set of ligands/receptors that are expressed in at least n% of the cells of this cell type
                    sig_expr_percentage = (
                        np.array((ct_signaling.X > 0).sum(axis=0)).squeeze() / ct_signaling.shape[0] * 100
                    )
                    ct_signaling = ct_signaling.var.index[sig_expr_percentage > self.target_expr_threshold]

                    sig_expr = pd.DataFrame(
                        self.adata[:, ct_signaling].X.A
                        if scipy.sparse.issparse(self.adata.X)
                        else self.adata[:, ct_signaling].X,
                        index=self.sample_names,
                        columns=ct_signaling,
                    )
                    signal[cell_type] = sig_expr

            else:
                raise ValueError(
                    "All of 'use_ligands', 'use_receptors', 'use_pathways', and 'use_cell_types' are False. Please set "
                    "at least one to True."
                )

            for subset_key in signal.keys():
                signal_values = signal[subset_key].values
                adata = subsets[subset_key]

                self.logger.info(
                    "Selecting transcription factors, cofactors and RNA-binding proteins for analysis of differential "
                    "expression."
                )

                # Further subset list of additional factors to those that are expressed in at least n% of the cells
                # that are nonzero in cells of interest (use the user input 'target_expr_threshold'):
                indices = np.any(signal_values != 0, axis=0).nonzero()[0]
                nz_signal = list(self.sample_names[indices])
                adata_subset = adata[nz_signal, :]
                n_cells_threshold = int(self.target_expr_threshold * adata_subset.n_obs)

                all_TFs = list(grn.columns)
                all_TFs = [tf for tf in all_TFs if tf in cof_db.columns and tf in tf_tf_db.columns]
                if scipy.sparse.issparse(adata.X):
                    nnz_counts = np.array(adata_subset[:, all_TFs].X.getnnz(axis=0)).flatten()
                else:
                    nnz_counts = np.array(adata_subset[:, all_TFs].X.getnnz(axis=0)).flatten()
                all_TFs = [tf for tf, nnz in zip(all_TFs, nnz_counts) if nnz >= n_cells_threshold]

                # Get the set of transcription cofactors that correspond to these transcription factors, in addition to
                # interacting transcription factors that may not themselves have passed the threshold:
                cof_subset = list(cof_db[(cof_db[all_TFs] == 1).any(axis=1)].index)
                cof_subset = [cof for cof in cof_subset if cof in self.feature_names]
                intersecting_tf_subset = list(tf_tf_db[(tf_tf_db[all_TFs] == 1).any(axis=1)].index)
                intersecting_tf_subset = [tf for tf in intersecting_tf_subset if tf in self.feature_names]

                # Subset to cofactors for which enough signal is present- filter to those expressed in at least n% of
                # the cells that express at least one of the TFs associated with the cofactor:
                all_cofactors = []
                for cofactor in cof_subset:
                    cof_row = cof_db.loc[cofactor, :]
                    cof_TFs = cof_row[cof_row == 1].index
                    tfs_expr_subset_indices = np.where(adata_subset[:, cof_TFs].X.sum(axis=1) > 0)[0]
                    tf_subset_cells = adata_subset[tfs_expr_subset_indices, :]
                    n_cells_threshold = int(self.target_expr_threshold * tf_subset_cells.n_obs)
                    if scipy.sparse.issparse(adata.X):
                        nnz_counts = np.array(tf_subset_cells[:, cofactor].X.getnnz(axis=0)).flatten()
                    else:
                        nnz_counts = np.array(tf_subset_cells[:, cofactor].X.getnnz(axis=0)).flatten()

                    if nnz_counts >= n_cells_threshold:
                        all_cofactors.append(cofactor)

                # And extend the set of transcription factors using interacting pairs that may also be present in the
                # same cells upstream transcription factors are:
                all_interacting_tfs = []
                for tf in intersecting_tf_subset:
                    tf_row = tf_tf_db.loc[tf, :]
                    tf_TFs = tf_row[tf_row == 1].index
                    tfs_expr_subset_indices = np.where(adata_subset[:, tf_TFs].X.sum(axis=1) > 0)[0]
                    tf_subset_cells = adata_subset[tfs_expr_subset_indices, :]
                    n_cells_threshold = int(self.target_expr_threshold * tf_subset_cells.n_obs)
                    if scipy.sparse.issparse(adata.X):
                        nnz_counts = np.array(tf_subset_cells[:, tf].X.getnnz(axis=0)).flatten()
                    else:
                        nnz_counts = np.array(tf_subset_cells[:, tf].X.getnnz(axis=0)).flatten()

                    if nnz_counts >= n_cells_threshold:
                        all_interacting_tfs.append(tf)

                # Do the same for RNA-binding proteins:
                all_RBPs = list(rna_bp_db["Gene_Name"].values)
                all_RBPs = [r for r in all_RBPs if r in self.feature_names]
                if len(all_RBPs) > 0:
                    if scipy.sparse.issparse(adata.X):
                        nnz_counts = np.array(adata_subset[:, all_RBPs].X.getnnz(axis=0)).flatten()
                    else:
                        nnz_counts = np.array(adata_subset[:, all_RBPs].X.getnnz(axis=0)).flatten()
                    all_RBPs = [tf for tf, nnz in zip(all_RBPs, nnz_counts) if nnz >= n_cells_threshold]
                    # Remove RBPs if any happen to be TFs or cofactors:
                    all_RBPs = [
                        r
                        for r in all_RBPs
                        if r not in all_TFs and r not in all_interacting_tfs and r not in all_cofactors
                    ]

                self.logger.info(f"For this dataset, marked {len(all_TFs)} of interest.")
                self.logger.info(
                    f"For this dataset, marked {len(all_cofactors)} transcriptional cofactors of interest."
                )
                if len(all_RBPs) > 0:
                    self.logger.info(f"For this dataset, marked {len(all_RBPs)} RNA-binding proteins of interest.")

                # Get feature names- for the singleton factors:
                regulator_features = all_TFs + all_interacting_tfs + all_cofactors + all_RBPs

                # Take subset of AnnData object corresponding to these regulators:
                counts = adata[:, regulator_features].copy()

                # Convert to dataframe:
                counts_df = pd.DataFrame(counts.X.toarray(), index=counts.obs_names, columns=counts.var_names)
                # combined_df = pd.concat([counts_df, signal[subset_key]], axis=1)

                # Store the targets (ligands/receptors) to AnnData object, save to file path:
                counts_targets = anndata.AnnData(scipy.sparse.csr_matrix(signal[subset_key].values))
                counts_targets.obs_names = signal[subset_key].index
                counts_targets.var_names = signal[subset_key].columns
                targets = signal[subset_key].columns
                # Make note that certain columns are pathways and not individual molecules that can be found in the
                # AnnData object:
                if use_pathways:
                    counts_targets.uns["target_type"] = "pathway"
                elif use_ligands or (use_cell_types and sender_receiver_or_target_degs == "sender"):
                    counts_targets.uns["target_type"] = "ligands"
                elif use_receptors or (use_cell_types and sender_receiver_or_target_degs == "receiver"):
                    counts_targets.uns["target_type"] = "receptors"
                elif use_targets or (use_cell_types and sender_receiver_or_target_degs == "target"):
                    counts_targets.uns["target_type"] = "target_genes"

                if compute_dim_reduction:
                    # To compute PCA, first need to standardize data:
                    sig_sub_df = signal[subset_key]
                    sig_sub_df = np.log1p(sig_sub_df)
                    sig_sub_df = (sig_sub_df - sig_sub_df.mean()) / sig_sub_df.std()

                    # Optionally, can use dimensionality reduction to aid in computing the nearest neighbors for the
                    # model (cells that are nearby in dimensionally-reduced signaling space will be neighbors in
                    # this scenario).
                    # Compute latent representation of the AnnData subset:

                    # Compute the ideal number of UMAP components to use- use half the number of features as the
                    # max possible number of components:
                    self.logger.info("Computing optimal number of PCA components ...")
                    n_pca_components = find_optimal_pca_components(sig_sub_df.values, TruncatedSVD)

                    # Perform UMAP reduction with the chosen number of components, store in AnnData object:
                    _, X_pca = pca_fit(sig_sub_df.values, TruncatedSVD, n_components=n_pca_components)
                    counts_targets.obsm["X_pca"] = X_pca
                    self.logger.info("Computed dimensionality reduction for gene expression targets.")

                # Compute the "Jaccard array" (recording expressed/not expressed):
                counts_targets.obsm["X_jaccard"] = np.where(signal[subset_key].values > 0, 1, 0)
                cell_types = self.adata.obs.loc[signal[subset_key].index, self.group_key]
                counts_targets.obs[group_key] = cell_types

                # Iterate over regulators:
                regulators = counts_df.columns
                # Add each target to AnnData .obs field:
                for reg in regulators:
                    counts_targets.obs[f"regulator_{reg}"] = counts_df[reg].values

                if "targets_path" in locals():
                    # Save to .txt file:
                    with open(targets_path, "w") as file:
                        for t in targets:
                            file.write(t + "\n")
                else:
                    if use_ligands or (use_cell_types and sender_receiver_or_target_degs == "sender"):
                        targets_path = os.path.join(targets_folder, f"{file_name}_{subset_key}_ligands.txt")
                    elif use_receptors or (use_cell_types and sender_receiver_or_target_degs == "receiver"):
                        targets_path = os.path.join(targets_folder, f"{file_name}_{subset_key}_receptors.txt")
                    elif use_pathways:
                        targets_path = os.path.join(targets_folder, f"{file_name}_{subset_key}_pathways.txt")
                    elif use_targets or (use_cell_types and sender_receiver_or_target_degs == "target"):
                        targets_path = os.path.join(targets_folder, f"{file_name}_{subset_key}_target_genes.txt")
                    with open(targets_path, "w") as file:
                        for t in targets:
                            file.write(t + "\n")

                if "ligand_set" in locals():
                    id = "ligand_regulators"
                elif "receptor_set" in locals():
                    id = "receptor_regulators"
                elif "target_set" in locals():
                    id = "target_gene_regulators"

                self.logger.info(
                    "'CCI_sender_deg_detection'- saving regulatory molecules to test as .h5ad file to the "
                    "directory of the output..."
                )
                counts_targets.write_h5ad(
                    os.path.join(output_dir, "cci_deg_detection", f"{file_name}_{subset_key}_{id}.h5ad")
                )

    def CCI_deg_detection(
        self,
        group_key: str,
        cci_dir_path: str,
        sender_receiver_or_target_degs: Literal["sender", "receiver", "target"] = "sender",
        use_ligands: bool = True,
        use_receptors: bool = False,
        use_pathways: bool = False,
        use_targets: bool = False,
        cell_type: Optional[str] = None,
        use_dim_reduction: bool = False,
        **kwargs,
    ):
        """Downstream method that when called, creates a separate instance of :class `MuSIC` specifically designed
        for the downstream task of detecting differentially expressed genes associated w/ ligand expression.

        Args:
            group_key: Key in `adata.obs` that corresponds to the cell type (or other grouping) labels
            cci_dir_path: Path to directory containing all Spateo databases
            sender_receiver_or_target_degs: Only makes a difference if 'use_pathways' or 'use_cell_types' is specified.
                Determines whether to compute DEGs for ligands, receptors or target genes. If 'use_pathways' is True,
                the value of this argument will determine whether ligands or receptors are used to define the model.
                Note that in either case, differential expression of TFs, binding factors, etc. will be computed in
                association w/ ligands/receptors/target genes (only valid if 'use_cell_types' and not 'use_pathways'
                is specified.
            use_ligands: Use ligand array for differential expression analysis. Will take precedent over receptors and
                sender/receiver cell types if also provided. Should match the input to :func
                `CCI_sender_deg_detection_setup`.
            use_receptors: Use receptor array for differential expression analysis.
            use_pathways: Use pathway array for differential expression analysis. Will use ligands in these pathways
                to collectively compute signaling potential score. Will take precedent over sender cell types if also
                provided. Should match the input to :func `CCI_sender_deg_detection_setup`.
            use_targets: Use target genes array for differential expression analysis.
            cell_type: Cell type to use to use for differential expression analysis. If given, will use the
                ligand/receptor subset obtained from :func ~`CCI_deg_detection_setup` and cells of the chosen
                cell type in the model.
            use_dim_reduction: Whether to use PCA representation of the data to find nearest neighbors. If False,
                will instead use the Jaccard distance. Defaults to False. Note that this will ultimately fail if
                dimensionality reduction was not performed in :func ~`CCI_deg_detection_setup`.
            kwargs: Keyword arguments for any of the Spateo argparse arguments. Should not include 'adata_path',
                'custom_lig_path' & 'ligand' or 'custom_pathways_path' & 'pathway' (depending on whether ligands or
                pathways are being used for the analysis), and should not include 'output_path' (which will be
                determined by the output path used for the main model). Should also not include any of the other
                arguments for this function

        Returns:
            downstream_model: Fitted model instance that can be used for further downstream applications
        """
        logger = lm.get_main_logger()

        kwargs["mod_type"] = "downstream"
        kwargs["cci_dir"] = cci_dir_path
        kwargs["group_key"] = group_key
        kwargs["coords_key"] = "X_pca" if use_dim_reduction else "X_jaccard"
        kwargs["bw_fixed"] = True

        # Use the same output directory as the main model, add folder demarcating results from downstream task:
        output_dir = os.path.dirname(self.output_path)
        output_file_name = os.path.basename(self.output_path)
        if not os.path.exists(os.path.join(output_dir, "cci_deg_detection")):
            os.makedirs(os.path.join(output_dir, "cci_deg_detection"))

        if use_ligands or use_receptors or use_pathways or use_targets:
            file_name = os.path.basename(self.adata_path).split(".")[0]
            if use_ligands:
                id = "ligand_regulators"
                file_id = "ligand_analysis"
            elif use_receptors:
                id = "receptor_regulators"
                file_id = "receptor_analysis"
            elif use_pathways and sender_receiver_or_target_degs == "sender":
                id = "ligand_regulators"
                file_id = "pathway_analysis_ligands"
            elif use_pathways and sender_receiver_or_target_degs == "receiver":
                id = "receptor_regulators"
                file_id = "pathway_analysis_receptors"
            elif use_targets:
                id = "target_gene_regulators"
                file_id = "target_gene_analysis"
            if not os.path.exists(os.path.join(output_dir, "cci_deg_detection", file_id)):
                os.makedirs(os.path.join(output_dir, "cci_deg_detection", file_id))
            output_path = os.path.join(output_dir, "cci_deg_detection", file_id, output_file_name)
            kwargs["output_path"] = output_path

            logger.info(
                f"Using AnnData object stored at "
                f"{os.path.join(output_dir, 'cci_deg_detection', f'{file_name}_all_{id}.h5ad')}."
            )
            kwargs["adata_path"] = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_{id}.h5ad")
            if use_ligands:
                kwargs["custom_lig_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_all_ligands.txt"
                )
                logger.info(f"Using ligands stored at {kwargs['custom_lig_path']}.")
            elif use_receptors:
                kwargs["custom_rec_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_all_receptors.txt"
                )
            elif use_pathways:
                kwargs["custom_pathways_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_all_pathways.txt"
                )
                logger.info(f"Using pathways stored at {kwargs['custom_pathways_path']}.")
            elif use_targets:
                kwargs["targets_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_all_target_genes.txt"
                )
                logger.info(f"Using target genes stored at {kwargs['targets_path']}.")
            else:
                raise ValueError("One of 'use_ligands', 'use_receptors', 'use_pathways' or 'use_targets' must be True.")

            # Create new instance of MuSIC:
            comm, parser, args_list = define_spateo_argparse(**kwargs)
            downstream_model = MuSIC(comm, parser, args_list)
            downstream_model._set_up_model()
            downstream_model.fit()
            downstream_model.predict_and_save()

        elif cell_type is not None:
            # For each cell type, fit a different model:
            file_name = os.path.basename(self.adata_path).split(".")[0]

            # create output sub-directory for this model:
            if sender_receiver_or_target_degs == "sender":
                file_id = "ligand_analysis"
            elif sender_receiver_or_target_degs == "receiver":
                file_id = "receptor_analysis"
            elif sender_receiver_or_target_degs == "target":
                file_id = "target_gene_analysis"
            if not os.path.exists(os.path.join(output_dir, "cci_deg_detection", cell_type, file_id)):
                os.makedirs(os.path.join(output_dir, "cci_deg_detection", cell_type, file_id))
            subset_output_dir = os.path.join(output_dir, "cci_deg_detection", cell_type, file_id)
            # Check if directory already exists, if not create it
            if not os.path.exists(subset_output_dir):
                self.logger.info(f"Output folder for cell type {cell_type} does not exist, creating it now.")
                os.makedirs(subset_output_dir)
            output_path = os.path.join(subset_output_dir, output_file_name)
            kwargs["output_path"] = output_path

            kwargs["adata_path"] = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_{cell_type}.h5ad")
            logger.info(f"Using AnnData object stored at {kwargs['adata_path']}.")
            if sender_receiver_or_target_degs == "sender":
                kwargs["custom_lig_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_ligands.txt"
                )
                logger.info(f"Using ligands stored at {kwargs['custom_lig_path']}.")
            elif sender_receiver_or_target_degs == "receiver":
                kwargs["custom_rec_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_receptors.txt"
                )
                logger.info(f"Using receptors stored at {kwargs['custom_rec_path']}.")
            elif sender_receiver_or_target_degs == "target":
                kwargs["targets_path"] = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_target_genes.txt"
                )
                logger.info(f"Using target genes stored at {kwargs['targets_path']}.")

            # Create new instance of MuSIC:
            comm, parser, args_list = define_spateo_argparse(**kwargs)
            downstream_model = MuSIC(comm, parser, args_list)
            downstream_model._set_up_model()
            downstream_model.fit()
            downstream_model.predict_and_save()

        else:
            raise ValueError("'use_ligands' and 'use_pathways' are both False, and 'cell_type' was not given.")

    def visualize_CCI_degs(
        self,
        plot_mode: Literal["proportion", "average"] = "proportion",
        sender_receiver_or_target_degs: Literal["sender", "receiver", "target"] = "sender",
        use_ligands: bool = True,
        use_receptors: bool = False,
        use_pathways: bool = False,
        use_targets: bool = False,
        cell_type: Optional[str] = None,
        fontsize: Union[None, int] = None,
        figsize: Union[None, Tuple[float, float]] = None,
        cmap: str = "seismic",
        save_show_or_return: Literal["save", "show", "return", "both", "all"] = "show",
        save_kwargs: Optional[dict] = {},
    ):
        """Visualize the result of downstream model that maps TFs/other regulatory genes to target genes.

        Args:
            plot_mode: Specifies what gets plotted.
                Options:
                    - "proportion": elements of the plot represent the proportion of total target-expressing cells
                        for which the given factor is predicted to have a nonzero effect
                    - "average": elements of the plot represent the average effect size across all target-expressing
                        cells
            sender_receiver_or_target_degs: Only makes a difference if 'use_pathways' or 'use_cell_types' is specified.
                Determines whether to compute DEGs for ligands, receptors or target genes. If 'use_pathways' is True,
                the value of this argument will determine whether ligands or receptors are used to define the model.
                Note that in either case, differential expression of TFs, binding factors, etc. will be computed in
                association w/ ligands/receptors/target genes (only valid if 'use_cell_types' and not 'use_pathways'
                is specified.
            use_ligands: Set True if this was True for the original model. Used to find the correct output location.
            use_receptors: Set True if this was True for the original model. Used to find the correct output location.
            use_pathways: Set True if this was True for the original model. Used to find the correct output location.
            use_targets: Set True if this was True for the original model. Used to find the correct output location.
            cell_type: Cell type of interest- should be the same as was provided to :func `CCI_deg_detection`.
            figsize: Width and height of plotting window
            cmap: Name of matplotlib colormap specifying colormap to use
            save_show_or_return: Whether to save, show or return the figure.
                If "both", it will save and plot the figure at the same time. If "all", the figure will be saved,
                displayed and the associated axis and other object will be return.
            save_kwargs: A dictionary that will passed to the save_fig function.
                By default it is an empty dictionary and the save_fig function will use the
                {"path": None, "prefix": 'scatter', "dpi": None, "ext": 'pdf', "transparent": True, "close": True,
                "verbose": True} as its parameters. Otherwise you can provide a dictionary that properly modifies those
                keys according to your needs.
        """
        config_spateo_rcParams()

        if fontsize is None:
            self.fontsize = rcParams.get("font.size")
        else:
            self.fontsize = fontsize
        if figsize is None:
            self.figsize = rcParams.get("figure.figsize")
        else:
            self.figsize = figsize

        output_dir = os.path.dirname(self.output_path)
        file_name = os.path.basename(self.adata_path).split(".")[0]

        # Load files for all targets:
        if use_ligands or use_receptors or use_pathways or use_targets:
            if use_ligands:
                file_id = "ligand_analysis"
                adata_id = "ligand_regulators"
                plot_id = "Target Ligand"
                title_id = "ligand"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_ligands.txt")
            elif use_receptors:
                file_id = "receptor_analysis"
                adata_id = "receptor_regulators"
                plot_id = "Target Receptor"
                title_id = "receptor"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_receptors.txt")
            elif use_targets:
                file_id = "target_gene_analysis"
                adata_id = "target_gene_regulators"
                plot_id = "Target Gene"
                title_id = "target gene"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_target_genes.txt")
            elif use_pathways and sender_receiver_or_target_degs == "sender":
                file_id = "pathway_analysis_ligands"
                adata_id = "ligand_regulators"
                plot_id = "Target Ligand"
                title_id = "ligand"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_pathways.txt")
            elif use_pathways and sender_receiver_or_target_degs == "receiver":
                file_id = "pathway_analysis_receptors"
                adata_id = "receptor_regulators"
                plot_id = "Target Receptor"
                title_id = "receptor"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_pathways.txt")
        elif cell_type is not None:
            if sender_receiver_or_target_degs == "sender":
                file_id = "ligand_analysis"
                adata_id = "ligand_regulators"
                plot_id = "Target Ligand"
                title_id = "ligand"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_ligands.txt")
            elif sender_receiver_or_target_degs == "receiver":
                file_id = "receptor_analysis"
                adata_id = "receptor_regulators"
                plot_id = "Target Receptor"
                title_id = "receptor"
                targets_path = os.path.join(output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_receptors.txt")
            elif sender_receiver_or_target_degs == "target":
                file_id = "target_analysis"
                adata_id = "target_gene_regulators"
                plot_id = "Target Gene"
                title_id = "target"
                targets_path = os.path.join(
                    output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_target_genes.txt"
                )
        else:
            raise ValueError(
                "'use_ligands', 'use_receptors', 'use_pathways' are all False, and 'cell_type' was not given."
            )
        contents_folder = os.path.join(output_dir, "cci_deg_detection", file_id)
        # Load list of targets:
        with open(targets_path, "r") as f:
            targets = [line.strip() for line in f.readlines()]
        # Complete list of regulatory factors- search through .obs of the AnnData object:
        if cell_type is None:
            adata = anndata.read_h5ad(os.path.join(output_dir, "cci_deg_detection", f"{file_name}_all_{adata_id}.h5ad"))
        else:
            adata = anndata.read_h5ad(
                os.path.join(output_dir, "cci_deg_detection", f"{file_name}_{cell_type}_{adata_id}.h5ad")
            )
        regulator_cols = [col.replace("regulator_", "") for col in adata.obs.columns if "regulator_" in col]

        # Compute proportion or average coefficients for all targets:
        # Load all targets files:
        target_files = {}

        for filename in os.listdir(contents_folder):
            # Check if any of the search strings are present in the filename
            for t in targets:
                if t in filename:
                    filepath = os.path.join(contents_folder, filename)
                    target_file = pd.read_csv(filepath, index_col=0)
                    target_file = target_file[
                        [c for c in target_file.columns if "b_" in c and "intercept" not in c]
                    ].copy()
                    target_file.columns = [c.replace("b_", "") for c in target_file.columns]
                    target_files[t] = target_file

        # Plot:
        fig, ax = plt.subplots(nrows=1, ncols=1, figsize=self.figsize)
        if plot_mode == "proportion":
            all_proportions = pd.DataFrame(0, index=regulator_cols, columns=targets)
            for t, target_df in target_files.items():
                nz_cells = np.where(adata[:, t].X.toarray() > 0)[0]
                proportions = (target_df.iloc[nz_cells] != 0).mean()
                all_proportions.loc[proportions.index, t] = proportions

            if all_proportions.shape[0] < all_proportions.shape[1]:
                to_plot = all_proportions.T
                xlabel = "Regulatory factor"
                ylabel = plot_id
            else:
                to_plot = all_proportions
                xlabel = plot_id
                ylabel = "Regulatory factor"

            mask = to_plot < 0.1
            hmap = sns.heatmap(
                to_plot,
                square=True,
                linecolor="grey",
                linewidths=0.3,
                cbar_kws={"label": "Proportion of cells", "location": "top", "shrink": 0.5},
                cmap=cmap,
                center=0.3,
                vmin=0.0,
                vmax=1.0,
                mask=mask,
                ax=ax,
            )

            # Adjust colorbar label font size
            cbar = hmap.collections[0].colorbar
            cbar.set_label("Proportion of cells", fontsize=self.fontsize * 1.1)
            # Adjust colorbar tick font size
            cbar.ax.tick_params(labelsize=self.fontsize)

            # Outer frame:
            for _, spine in hmap.spines.items():
                spine.set_visible(True)
                spine.set_linewidth(0.75)

            plt.xlabel(xlabel, fontsize=self.fontsize * 1.1)
            plt.ylabel(ylabel, fontsize=self.fontsize * 1.1)
            plt.xticks(fontsize=self.fontsize)
            plt.yticks(fontsize=self.fontsize)
            plt.title(
                f"Preponderance of inferred \n regulatory effect on {title_id} expression",
                fontsize=self.fontsize * 1.25,
            ) if cell_type is None else plt.title(
                f"Preponderance of inferred regulatory \n effect on {title_id} expression in {cell_type}",
                fontsize=self.fontsize * 1.25,
            )
            plt.tight_layout()

        elif plot_mode == "average":
            all_averages = pd.DataFrame(0, index=regulator_cols, columns=targets)
            for t, target_df in target_files.items():
                nz_cells = np.where(adata[:, t].X.toarray() > 0)[0]
                averages = target_df.iloc[nz_cells].mean()
                all_averages.loc[averages.index, t] = averages

            if all_averages.shape[0] < all_averages.shape[1]:
                to_plot = all_averages.T
                xlabel = "Regulatory factor"
                ylabel = plot_id
            else:
                to_plot = all_averages
                xlabel = plot_id
                ylabel = "Regulatory factor"

            q40 = np.percentile(all_averages.values.flatten(), 40)
            q20 = np.percentile(all_averages.values.flatten(), 20)
            mask = to_plot < q20
            hmap = sns.heatmap(
                to_plot,
                square=True,
                linecolor="grey",
                linewidths=0.3,
                cbar_kws={"label": "Average effect size", "location": "top", "shrink": 0.5},
                cmap=cmap,
                center=q40,
                vmin=0.0,
                vmax=1.0,
                mask=mask,
                ax=ax,
            )

            # Adjust colorbar label font size
            cbar = hmap.collections[0].colorbar
            cbar.set_label("Average effect size", fontsize=self.fontsize * 1.1)
            # Adjust colorbar tick font size
            cbar.ax.tick_params(labelsize=self.fontsize)

            # Outer frame:
            for _, spine in hmap.spines.items():
                spine.set_visible(True)
                spine.set_linewidth(0.75)

            plt.xlabel(xlabel, fontsize=self.fontsize * 1.1)
            plt.ylabel(ylabel, fontsize=self.fontsize * 1.1)
            plt.xticks(fontsize=self.fontsize)
            plt.yticks(fontsize=self.fontsize)
            plt.title(
                f"Average inferred \n regulatory effects on {title_id} expression", fontsize=self.fontsize * 1.25
            ) if cell_type is None else plt.title(
                f"Average inferred regulatory effects on {title_id} expression in {cell_type}",
                fontsize=self.fontsize * 1.25,
            )
            plt.tight_layout()

            save_return_show_fig_utils(
                save_show_or_return=save_show_or_return,
                show_legend=True,
                background="white",
                prefix=f"{plot_mode}_{file_name}_{file_id}",
                save_kwargs=save_kwargs,
                total_panels=1,
                fig=fig,
                axes=ax,
                return_all=False,
                return_all_list=None,
            )

    def visualize_intercellular_network(
        self,
        lr_model_output_dir: str,
        target_subset: Optional[Union[List[str], str]] = None,
        ligand_subset: Optional[Union[List[str], str]] = None,
        receptor_subset: Optional[Union[List[str], str]] = None,
        regulator_subset: Optional[Union[List[str], str]] = None,
        include_tf_ligand: bool = False,
        include_tf_receptor: bool = False,
        include_tf_target: bool = True,
        cell_subset: Optional[Union[List[str], str]] = None,
        select_n_lr: int = 5,
        select_n_tf: int = 3,
        subset_ligand_expr_threshold: float = 0.2,
        cmap_neighbors: str = "YlOrRd",
        cmap_default: str = "YlGnBu",
        scale_factor: float = 3,
        layout: Literal["random", "circular", "kamada", "planar", "spring", "spectral", "spiral"] = "planar",
        save_path: Optional[str] = None,
        save_id: Optional[str] = None,
        save_ext: str = "png",
        dpi: int = 300,
        **kwargs,
    ):
        """After fitting model, construct and visualize the inferred intercellular regulatory network. Effect sizes (
        edge values) will be averaged over cells specified by "cell_subset", otherwise all cells will be used.

        Args:
            lr_model_output_dir: Path to directory containing the outputs of the L:R model. This function will assume
                :attr `output_path` is the output path for the downstream model, i.e. connecting regulatory
                factors/TFs to ligands/receptors/targets.
            target_subset: Optional, can be used to specify target genes downstream of signaling interactions of
                interest. If not given, will use all targets used for the model.
            ligand_subset: Optional, can be used to specify subset of ligands. If not given, will use all ligands
                present in any of the interactions for the model.
            receptor_subset: Optional, can be used to specify subset of receptors. If not given, will use all receptors
                present in any of the interactions for the model.
            regulator_subset: Optional, can be used to specify subset of regulators (transcription factors,
                etc.). If not given, will use all regulatory molecules used in fitting the downstream model(s).
            include_tf_ligand: Whether to include TF-ligand interactions in the network. While providing more
                information, this can make it more difficult to interpret the plot. Defaults to False.
            include_tf_receptor: Whether to include TF-receptor interactions in the network. While providing more
                information, this can make it more difficult to interpret the plot. Defaults to False.
            include_tf_target: Whether to include TF-target interactions in the network. While providing more
                information, this can make it more difficult to interpret the plot. Defaults to True.
            cell_subset: Optional, can be used to specify subset of cells to use for averaging effect sizes. If not
                given, will use all cells. Can be either:
                    - A list of cell IDs (must be in the same format as the cell IDs in the adata object)
                    - Cell type label(s)
            select_n_lr: Threshold for filtering out edges with low effect sizes, by selecting up to the top n L:R
                interactions per target (fewer can be selected if the top n are all zero). Default is 5.
            select_n_tf: Threshold for filtering out edges with low effect sizes, by selecting up to the top n
                TF-ligand, TF-receptor and/or TF-target relationships (fewer can be selected if the top n are all
                zero). Default is 3.
            subset_ligand_expr_threshold: For the specified cell subset, this threshold will be used to filter out
                ligands- only those expressed in above this threshold proportion of cells will be kept.
            cmap_neighbors: Colormap to use for nodes belonging to "source"/receiver cells. Defaults to
                yellow-orange-red.
            cmap_default: Colormap to use for nodes belonging to "neighbor"/sender cells. Defaults to
                purple-blue-green.
            scale_factor: Adjust to modify the size of the nodes
            layout: Used for positioning nodes on the plot. Options:
                - "random": Randomly positions nodes ini the unit square.
                - "circular": Positions nodes on a circle.
                - "kamada": Positions nodes using Kamada-Kawai path-length cost-function.
                - "planar": Positions nodes without edge intersections, if possible.
                - "spring": Positions nodes using Fruchterman-Reingold force-directed algorithm.
                - "spectral": Positions nodes using eigenvectors of the graph Laplacian.
                - "spiral": Positions nodes in a spiral layout.
            save_path: Optional, directory to save figure to. If not given, will save to the parent folder of the
                path provided for :attr `output_path` in the argument specification.
            save_id: Optional unique identifier that can be used in saving. If not given, will use the AnnData
                object path to derive this.
            save_ext: File extension to save figure as. Default is "png".
            dpi: Resolution to save figure at. Default is 300.
            **kwargs: NOT CURRENTLY INCLUDED. Additional arguments that can be provided to :func igviz.plot(); note
                that 'color_method', 'node_label' and 'edge_label' should not be provided this way. Notable options
                include:
                    - node_label_position: Position of the node label relative to the node. Either {'top left',
                        'top center', 'top right', 'middle left', 'middle center', 'middle right', 'bottom left',
                        'bottom center', 'bottom right'}
                    - edge_label_position: Position of the edge label relative to the edge. Either {'top left',
                        'top center', 'top right', 'middle left', 'middle center', 'middle right', 'bottom left',
                        'bottom center', 'bottom right'}

        Returns:
            G: Graph object, such that it can be separately plotted in interactive window.
            sizing_list: List of node sizes, for use in interactive window.
            color_list: List of node colors, for use in interactive window.
        """

        logger = lm.get_main_logger()
        config_spateo_rcParams()
        # Set display DPI:
        plt.rcParams["figure.dpi"] = dpi

        # Check that self.output_path corresponds to the downstream model if "regulator_subset" is given:
        downstream_model_output_dir = os.path.dirname(self.output_path)
        if (
            not os.path.exists(os.path.join(downstream_model_output_dir, "cci_deg_detection"))
            and regulator_subset is not None
        ):
            raise FileNotFoundError(
                "No downstream model was ever constructed, however this is necessary to include "
                "regulatory factors in the network."
            )

        # Check that lr_model_output_dir points to the correct folder for the L:R model- to do this check for
        # predictions file directly in the folder (for downstream models, predictions are further nested in the
        # "cci_deg_detection" derived subdirectories):
        if not os.path.exists(os.path.join(lr_model_output_dir, "predictions.csv")):
            raise FileNotFoundError(
                "Check that provided `lr_model_output_dir` points to the correct folder for the "
                "L:R model. For example, if the specified model output path is "
                "outer/folder/results.csv, this should be outer/folder."
            )
        lr_model_output_files = os.listdir(lr_model_output_dir)
        # Get L:R names from the design matrix:
        for file in lr_model_output_files:
            path = os.path.join(lr_model_output_dir, file)
            if os.path.isdir(path):
                if file not in ["analyses", "significance", "networks", ".ipynb_checkpoints"]:
                    design_mat = pd.read_csv(os.path.join(path, "design_matrix", "design_matrix.csv"), index_col=0)
        lr_to_target_feature_names = design_mat.columns.tolist()

        # If subset for ligands and/or receptors is not specified, use all that were included in the model:
        if ligand_subset is None:
            ligand_subset = []
        if receptor_subset is None:
            receptor_subset = []

        for lig_rec in lr_to_target_feature_names:
            lig, rec = lig_rec.split(":")
            lig_split = lig.split("/")
            for l in lig_split:
                if l not in ligand_subset:
                    ligand_subset.append(l)

            if rec not in receptor_subset:
                receptor_subset.append(rec)

        downstream_model_dir = os.path.dirname(self.output_path)

        # Get the names of target genes from the L:R-to-target model from input to lr_model_output_dir:
        all_targets = []
        target_to_file = {}
        for file in lr_model_output_files:
            if file.endswith(".csv") and "predictions" not in file:
                parts = file.split("_")
                target_str = parts[-1].replace(".csv", "")
                # And map the target to the file name:
                target_to_file[target_str] = os.path.join(lr_model_output_dir, file)
                all_targets.append(target_str)

        # Check if any downstream models exist (TF-ligand, TF-receptor, or TF-target):
        if os.path.exists(os.path.join(downstream_model_dir, "cci_deg_detection")):
            if os.path.exists(os.path.join(downstream_model_dir, "cci_deg_detection", "ligand_analysis")):
                # Get the names of target genes from the TF-to-ligand model by looking within the output directory
                # containing :attr `output_path`:
                all_modeled_ligands = []
                ligand_to_file = {}
                ligand_folder = os.path.join(downstream_model_dir, "cci_deg_detection", "ligand_analysis")
                ligand_files = os.listdir(ligand_folder)
                for file in ligand_files:
                    if file.endswith(".csv"):
                        parts = file.split("_")
                        ligand_str = parts[-1].replace(".csv", "")
                        # And map the ligand to the file name:
                        ligand_to_file[ligand_str] = os.path.join(ligand_folder, file)
                        all_modeled_ligands.append(ligand_str)

                # Get TF names from the design matrix:
                for file in ligand_files:
                    path = os.path.join(ligand_folder, file)
                    if file != ".ipynb_checkpoints":
                        if os.path.isdir(path):
                            design_mat = pd.read_csv(
                                os.path.join(path, "downstream_design_matrix", "design_matrix.csv"), index_col=0
                            )
                tf_to_ligand_feature_names = [col.replace("regulator_", "") for col in design_mat.columns]

            if os.path.exists(os.path.join(downstream_model_dir, "cci_deg_detection", "receptor_analysis")):
                # Get the names of target genes from the TF-to-receptor model by looking within the output directory
                # containing :attr `output_path`:
                all_modeled_receptors = []
                receptor_to_file = {}
                receptor_folder = os.path.join(downstream_model_dir, "cci_deg_detection", "receptor_analysis")
                receptor_files = os.listdir(receptor_folder)
                for file in receptor_files:
                    if file.endswith(".csv"):
                        parts = file.split("_")
                        receptor_str = parts[-1].replace(".csv", "")
                        # And map the receptor to the file name:
                        receptor_to_file[receptor_str] = os.path.join(receptor_folder, file)
                        all_modeled_receptors.append(receptor_str)

                # Get TF names from the design matrix:
                for file in receptor_files:
                    path = os.path.join(receptor_folder, file)
                    if file != ".ipynb_checkpoints":
                        if os.path.isdir(path):
                            design_mat = pd.read_csv(
                                os.path.join(path, "downstream_design_matrix", "design_matrix.csv"), index_col=0
                            )
                tf_to_receptor_feature_names = [col.replace("regulator_", "") for col in design_mat.columns]

            if os.path.exists(os.path.join(downstream_model_dir, "cci_deg_detection", "target_gene_analysis")):
                # Get the names of target genes from the TF-to-target model by looking within the output directory
                # containing :attr `output_path`:
                all_modeled_targets = []
                modeled_target_to_file = {}
                target_folder = os.path.join(downstream_model_dir, "cci_deg_detection", "target_gene_analysis")
                target_files = os.listdir(target_folder)
                for file in target_files:
                    if file.endswith(".csv"):
                        parts = file.split("_")
                        target_str = parts[-1].replace(".csv", "")
                        # And map the target to the file name:
                        modeled_target_to_file[target_str] = os.path.join(target_folder, file)
                        all_modeled_targets.append(target_str)

                # Get TF names from the design matrix:
                for file in target_files:
                    path = os.path.join(target_folder, file)
                    if file != ".ipynb_checkpoints":
                        if os.path.isdir(path):
                            design_mat = pd.read_csv(
                                os.path.join(path, "downstream_design_matrix", "design_matrix.csv"), index_col=0
                            )
                tf_to_target_feature_names = [col.replace("regulator_", "") for col in design_mat.columns]

        if save_path is not None:
            save_folder = os.path.join(os.path.dirname(save_path), "networks")
        else:
            save_folder = os.path.join(os.path.dirname(self.output_path), "networks")

        if not os.path.exists(save_folder):
            os.makedirs(save_folder)

        if cell_subset is not None:
            if all(label in set(self.adata.obs[self.group_key]) for label in cell_subset):
                adata = self.adata[self.adata.obs[self.group_key].isin(cell_subset)].copy()
                # Get numerical indices corresponding to cells in the subset:
                cell_ids = [i for i, name in enumerate(self.adata.obs_names) if name in adata.obs_names]
            else:
                adata = self.adata[cell_subset, :].copy()
                cell_ids = [i for i, name in enumerate(self.adata.obs_names) if name in adata.obs_names]
        else:
            adata = self.adata.copy()
            cell_ids = [i for i, name in enumerate(self.adata.obs_names)]

        targets = all_targets if target_subset is None else target_subset

        # Check for existing dataframes that will be used to construct the network:
        if os.path.exists(os.path.join(save_folder, "lr_to_target.csv")):
            lr_to_target_df = pd.read_csv(os.path.join(save_folder, "lr_to_target.csv"), index_col=0)
        else:
            # Construct L:R-to-target dataframe:
            lr_to_target_df = pd.DataFrame(0, index=lr_to_target_feature_names, columns=targets)

            for target in targets:
                # Load file corresponding to this target:
                file_name = target_to_file[target]
                file_path = os.path.join(lr_model_output_dir, file_name)
                target_df = pd.read_csv(file_path, index_col=0)
                target_df = target_df.loc[:, [col for col in target_df.columns if col.startswith("b_")]]
                # Compute average predicted effect size over the chosen cell subset to populate L:R-to-target
                # dataframe:
                target_df.columns = [col.replace("b_", "") for col in target_df.columns if col.startswith("b_")]
                lr_to_target_df.loc[:, target] = target_df.iloc[cell_ids, :].mean(axis=0)

            # Save L:R-to-target dataframe:
            lr_to_target_df.to_csv(os.path.join(save_folder, "lr_to_target.csv"))

        # Construct TF-to-ligand/receptor/target dataframes if needed:
        if "tf_to_ligand_feature_names" in locals():
            if os.path.exists(os.path.join(save_folder, "tf_to_ligand.csv")):
                tf_to_ligand_df = pd.read_csv(os.path.join(save_folder, "tf_to_ligand.csv"), index_col=0)
            else:
                # Construct TF to ligand dataframe:
                tf_to_ligand_df = pd.DataFrame(0, index=tf_to_ligand_feature_names, columns=all_modeled_ligands)

                for ligand in all_modeled_ligands:
                    file_name = ligand_to_file[ligand]
                    file_path = os.path.join(downstream_model_dir, "cci_deg_detection", "ligand_analysis", file_name)
                    ligand_df = pd.read_csv(file_path, index_col=0)
                    ligand_df = ligand_df.loc[:, [col for col in ligand_df.columns if col.startswith("b_")]]
                    # Compute average predicted effect size over the chosen cell subset to populate the TF-to-ligand
                    # dataframe:
                    ligand_df.columns = [col.replace("b_", "") for col in ligand_df.columns if col.startswith("b_")]
                    tf_to_ligand_df.loc[:, ligand] = ligand_df.iloc[cell_ids, :].mean(axis=0)

                # Save TF-to-ligand dataframe:
                tf_to_ligand_df.to_csv(os.path.join(save_folder, "tf_to_ligand.csv"))

        if "tf_to_receptor_feature_names" in locals():
            if os.path.exists(os.path.join(save_folder, "tf_to_receptor.csv")):
                tf_to_receptor_df = pd.read_csv(os.path.join(save_folder, "tf_to_receptor.csv"), index_col=0)
            else:
                # Construct TF to receptor dataframe:
                tf_to_receptor_df = pd.DataFrame(0, index=tf_to_receptor_feature_names, columns=all_modeled_receptors)

                for receptor in all_modeled_receptors:
                    file_name = receptor_to_file[receptor]
                    file_path = os.path.join(downstream_model_dir, "cci_deg_detection", "receptor_analysis", file_name)
                    receptor_df = pd.read_csv(file_path, index_col=0)
                    receptor_df = receptor_df.loc[:, [col for col in receptor_df.columns if col.startswith("b_")]]
                    # Compute average predicted effect size over the chosen cell subset to populate the TF-to-receptor
                    # dataframe:
                    receptor_df.columns = [col.replace("b_", "") for col in receptor_df.columns if col.startswith("b_")]
                    tf_to_receptor_df.loc[:, receptor] = receptor_df.iloc[cell_ids, :].mean(axis=0)

                # Save TF-to-receptor dataframe:
                tf_to_receptor_df.to_csv(os.path.join(save_folder, "tf_to_receptor.csv"))

        if "tf_to_target_feature_names" in locals():
            if os.path.exists(os.path.join(save_folder, "tf_to_target.csv")):
                tf_to_target_df = pd.read_csv(os.path.join(save_folder, "tf_to_target.csv"), index_col=0)
            else:
                # Construct TF to target dataframe:
                tf_to_target_df = pd.DataFrame(0, index=tf_to_target_feature_names, columns=targets)

                for target in targets:
                    file_name = target_to_file[target]
                    file_path = os.path.join(downstream_model_dir, "cci_deg_detection", "target_analysis", file_name)
                    target_df = pd.read_csv(file_path, index_col=0)
                    target_df = target_df.loc[:, [col for col in target_df.columns if col.startswith("b_")]]
                    # Compute average predicted effect size over the chosen cell subset to populate the TF-to-target
                    # dataframe:
                    target_df.columns = [col.replace("b_", "") for col in target_df.columns if col.startswith("b_")]
                    tf_to_target_df.loc[:, target] = target_df.iloc[cell_ids, :].mean(axis=0)

                # Save TF-to-target dataframe:
                tf_to_target_df.to_csv(os.path.join(save_folder, "tf_to_target.csv"))

        # Graph construction:
        G = nx.DiGraph()

        # Identify nodes and edges from L:R-to-target dataframe:
        for target in targets:
            top_n_lr = lr_to_target_df.nlargest(n=select_n_lr, columns=target).index.tolist()
            # Or check if any of the top n should reasonably not be included- compare to :attr target_expr_threshold
            # of the maximum in the array (because these values can be variable):
            reference_value = lr_to_target_df.max().max()
            top_n_lr = [
                lr
                for lr in top_n_lr
                if lr_to_target_df.loc[lr, target] >= (self.target_expr_threshold * reference_value)
            ]

            target = f"Target: {target}"
            if not G.has_node(target):
                G.add_node(target, ID=target)

            for lr in top_n_lr:
                ligand_receptor_pair = lr
                ligands, receptor = ligand_receptor_pair.split(":")

                # Check if ligands and receptors are in their respective subsets
                if receptor in receptor_subset and any(lig in ligand_subset for lig in ligands.split("/")):
                    # For ligands separated by "/", check expression of each individual ligand in the AnnData object,
                    # keep ligands that are sufficiently expressed in the specified cell subset:
                    for lig in ligands.split("/"):
                        num_expr = (adata[:, lig].X > 0).sum()
                        expr_percent = (num_expr / adata.shape[0]) * 100
                        pass_threshold = expr_percent >= subset_ligand_expr_threshold

                        if lig in ligand_subset and pass_threshold:
                            # For the intents of this network, the ligand refers to ligand expressed in neighboring
                            # cells:
                            lig = f"Neighbor {lig}"
                            if not G.has_node(lig):
                                G.add_node(lig, ID=lig)
                            if not G.has_node(receptor):
                                G.add_node(receptor, ID=receptor)
                            G.add_edge(lig, receptor, Type="L:R")

                    # Add edge from receptor to target with the DataFrame value as property:
                    G.add_edge(receptor, target, Type="L:R effect")

        # Check which of the downstream models (if any) were run, load the corresponding files and add to the network:
        if "tf_to_ligand_df" in locals() and include_tf_ligand:
            if regulator_subset is None:
                regulator_subset = tf_to_ligand_df.index

            for ligand in tf_to_ligand_df.columns:
                node_ligand_label = f"Neighbor {ligand}"
                top_n_tf = tf_to_ligand_df.nlargest(n=select_n_tf, columns=ligand).index.tolist()
                # Or check if any of the top n should reasonably not be included- compare to :attr target_expr_threshold
                # of the maximum in the array (because these values can be variable):
                reference_value = tf_to_ligand_df.max().max()
                top_n_tf = [
                    tf
                    for tf in top_n_tf
                    if tf_to_ligand_df.loc[tf, ligand] >= (self.target_expr_threshold * reference_value)
                ]

                for tf in top_n_tf:
                    # Check if ligand is in the ligand subset:
                    if ligand in ligand_subset and G.has_node(node_ligand_label):
                        # For the intents of this network, the TF refers to TF expressed in neighboring cells:
                        tf = f"Neighbor {tf}"
                        ligand = f"Neighbor {ligand}"
                        if not G.has_node(tf):
                            G.add_node(tf, ID=tf)
                        G.add_edge(tf, ligand, Type="TF:Ligand")

        if "tf_to_receptor_df" in locals() and include_tf_receptor:
            if regulator_subset is None:
                regulator_subset = tf_to_receptor_df.index

            for receptor in tf_to_receptor_df.columns:
                top_n_tf = tf_to_receptor_df.nlargest(n=select_n_tf, columns=receptor).index.tolist()
                # Or check if any of the top n should reasonably not be included- compare to :attr target_expr_threshold
                # of the maximum in the array (because these values can be variable):
                reference_value = tf_to_receptor_df.max().max()
                top_n_tf = [
                    tf
                    for tf in top_n_tf
                    if tf_to_receptor_df.loc[tf, receptor] >= (self.target_expr_threshold * reference_value)
                ]

                for tf in top_n_tf:
                    # Check if receptor is in the receptor subset:
                    if receptor in receptor_subset and G.has_node(receptor):
                        if not G.has_node(tf):
                            G.add_node(tf, ID=tf)
                        G.add_edge(tf, receptor, Type="TF:Receptor")

        if "tf_to_target_df" in locals() and include_tf_target:
            if regulator_subset is None:
                regulator_subset = tf_to_target_df.index

            for target in tf_to_target_df.columns:
                top_n_tf = tf_to_target_df.nlargest(n=select_n_tf, columns=target).index.tolist()
                # Or check if any of the top n should reasonably not be included- compare to :attr target_expr_threshold
                # of the maximum in the array (because these values can be variable):
                reference_value = tf_to_target_df.max().max()
                top_n_tf = [
                    tf
                    for tf in top_n_tf
                    if tf_to_target_df.loc[tf, target] >= (self.target_expr_threshold * reference_value)
                ]

                for tf in top_n_tf:
                    if G.has_node(f"Target: {target}"):
                        if not G.has_node(tf):
                            G.add_node(tf, ID=tf)
                        G.add_edge(tf, target, Type="TF:Target")

        # Set colors for nodes- for neighboring cell ligands + TFs, use a distinct colormap (and same w/ receptors,
        # targets and TFs for source cells)- color both on gradient based on number of connections:
        color_list = []
        sizing_list = []
        sizing_neighbor = {}
        sizing_nonneighbor = {}

        cmap_neighbor = plt.cm.get_cmap(cmap_neighbors)
        cmap_non_neighbor = plt.cm.get_cmap(cmap_default)

        # Calculate node degrees and set color and size based on the degree and label
        for node in G.nodes():
            degree = G.degree(node)
            # Add degree as property:
            G.nodes[node]["Connections"] = degree
            size_and_color = np.sqrt(degree) * scale_factor

            # Add size to sizing_list
            if "Neighbor" in node:
                sizing_neighbor[node] = size_and_color
            else:
                sizing_nonneighbor[node] = size_and_color

        for node in G.nodes():
            if "Neighbor" in node:
                color = matplotlib.colors.to_hex(cmap_neighbor(sizing_neighbor[node] / max(sizing_neighbor.values())))
                sizing_list.append(sizing_neighbor[node])
            else:
                color = matplotlib.colors.to_hex(
                    cmap_non_neighbor(sizing_nonneighbor[node] / max(sizing_nonneighbor.values()))
                )
                sizing_list.append(sizing_nonneighbor[node])
            color_list.append(color)

        if layout == "planar":
            is_planar, _ = nx.check_planarity(G)
            if not is_planar:
                logger.info("Graph is not planar, using spring layout instead.")
                layout = "spring"

        # Draw graph:
        f = ig.plot(
            G,
            size_method=sizing_list,
            color_method=color_list,
            node_text=["Connections"],
            node_label="ID",
            node_label_position="top center",
            edge_text=["Type"],
            # edge_label="Type",
            # edge_label_position="bottom center",
            layout=layout,
            arrow_size=1,
        )

        # Save graph:
        if save_id is None:
            save_id = os.path.basename(self.adata_path).split(".")[0]
        if save_path is None:
            save_path = save_folder
        full_save_path = os.path.join(save_path, f"{save_id}_network.{save_ext}")
        logger.info(f"Writing network to {full_save_path}...")

        fig = plotly.graph_objects.Figure(f)
        fig.update_layout(margin=dict(b=20, l=20, r=20, t=40))
        # The default is 100 DPI
        fig.write_image(full_save_path, scale=dpi / 100)

        return G, sizing_list, color_list

    # ---------------------------------------------------------------------------------------------------
    # Permutation testing
    # ---------------------------------------------------------------------------------------------------
    def permutation_test(self, gene: str, n_permutations: int = 100, permute_nonzeros_only: bool = False, **kwargs):
        """Sets up permutation test for determination of statistical significance of model diagnostics. Can be used
        to identify true/the strongest signal-responsive expression patterns.

        Args:
            gene: Target gene to perform permutation test on.
            n_permutations: Number of permutations of the gene expression to perform. Default is 100.
            permute_nonzeros_only: Whether to only perform the permutation over the gene-expressing cells
            kwargs: Keyword arguments for any of the Spateo argparse arguments. Should not include 'adata_path',
                'target_path', or 'output_path' (which will be determined by the output path used for the main
                model). Also should not include 'custom_lig_path', 'custom_rec_path', 'mod_type', 'bw_fixed' or 'kernel'
                (which will be determined by the initial model instantiation).
        """

        # Set up storage folder:
        # Check if the array of additional molecules to query has already been created:
        parent_dir = os.path.dirname(self.adata_path)
        file_name = os.path.basename(self.adata_path).split(".")[0]

        if not os.path.exists(os.path.join(parent_dir, "permutation_test")):
            os.makedirs(os.path.join(parent_dir, "permutation_test"))
        if not os.path.exists(os.path.join(parent_dir, "permutation_test_inputs")):
            os.makedirs(os.path.join(parent_dir, "permutation_test_inputs"))
        if not os.path.exists(os.path.join(parent_dir, f"permutation_test_outputs_{gene}")):
            os.makedirs(os.path.join(parent_dir, f"permutation_test_outputs_{gene}"))

        gene_idx = self.adata.var_names.tolist().index(gene)
        gene_data = np.array(self.adata.X[:, gene_idx].todense())

        permuted_data_list = [gene_data]
        perm_names = [f"{gene}_nonpermuted"]

        # Set save name for AnnData object and output file depending on whether all cells or only gene-expressing
        # cells are permuted:
        if permute_nonzeros_only:
            adata_path = os.path.join(
                parent_dir, "permutation_test", f"{file_name}_{gene}_permuted_expressing_subset.h5ad"
            )
            output_path = os.path.join(
                parent_dir, "permutation_test_outputs", f"{file_name}_{gene}_permuted_expressing_subset.csv"
            )
            self.permuted_nonzeros_only = True
        else:
            adata_path = os.path.join(parent_dir, "permutation_test", f"{file_name}_{gene}_permuted.h5ad")
            output_path = os.path.join(parent_dir, "permutation_test_outputs", f"{file_name}_{gene}_permuted.csv")
            self.permuted_nonzeros_only = False

        if permute_nonzeros_only:
            self.logger.info("Performing permutation by scrambling expression for all cells...")
            for i in range(n_permutations):
                perm_name = f"{gene}_permuted_{i}"
                permuted_data = np.random.permutation(gene_data)
                # Convert to sparse matrix
                permuted_data_sparse = scipy.sparse.csr_matrix(permuted_data)

                # Store back in the AnnData object
                permuted_data_list.append(permuted_data_sparse)
                perm_names.append(perm_name)
        else:
            self.logger.info(
                "Performing permutation by scrambling expression only for the subset of cells that "
                "express the gene of interest..."
            )
            for i in range(n_permutations):
                perm_name = f"{gene}_permuted_{i}"
                # Separate non-zero rows and zero rows:
                nonzero_indices = np.where(gene_data != 0)[0]
                zero_indices = np.where(gene_data == 0)[0]

                non_zero_rows = gene_data[gene_data != 0]
                zero_rows = gene_data[gene_data == 0]

                # Permute non-zero rows:
                permuted_non_zero_rows = np.random.permutation(non_zero_rows)

                # Recombine permuted non-zero rows and zero rows:
                permuted_gene_data = np.zeros_like(gene_data)
                permuted_gene_data[nonzero_indices] = permuted_non_zero_rows.reshape(-1, 1)
                permuted_gene_data[zero_indices] = zero_rows.reshape(-1, 1)
                # Convert to sparse matrix
                permuted_gene_data_sparse = scipy.sparse.csr_matrix(permuted_gene_data)

                # Store back in the AnnData object
                permuted_data_list.append(permuted_gene_data_sparse)
                perm_names.append(perm_name)

        # Concatenate the original and permuted data:
        all_data_sparse = scipy.sparse.hstack([self.adata.X] + permuted_data_list)
        all_data_sparse = all_data_sparse.tocsr()
        all_names = list(self.adata.var_names.tolist() + perm_names)

        # Create new AnnData object, keeping the cell type annotations, original "__type" entry, and all .obsm
        # entries (including spatial coordinates):
        adata_permuted = anndata.AnnData(X=all_data_sparse)
        adata_permuted.obs_names = self.adata.obs_names
        adata_permuted.var_names = all_names
        adata_permuted.obsm = self.adata.obsm
        adata_permuted.obs[self.group_key] = self.adata.obs[self.group_key]
        adata_permuted.obs["__type"] = self.adata.obs["__type"]

        # Save list of targets:
        targets = [v for v in adata_permuted.var_names if "permuted" in v]
        target_path = os.path.join(parent_dir, "permutation_test_inputs", f"{gene}_permutation_targets.txt")
        with open(target_path, "w") as f:
            for target in targets:
                f.write(f"{target}\n")
        # Save the permuted AnnData object:
        adata_permuted.write(adata_path)

        # Fitting permutation model:
        kwargs["adata_path"] = adata_path
        kwargs["output_path"] = output_path
        kwargs["cci_dir"] = self.cci_dir
        if hasattr(self, "custom_receptors_path") and self.mod_type.isin(["receptor", "lr"]):
            kwargs["custom_rec_path"] = self.custom_receptors_path
        elif hasattr(self, "custom_pathways_path") and self.mod_type.isin(["receptor", "lr"]):
            kwargs["custom_pathways_path"] = self.custom_pathways_path
        else:
            raise ValueError("For permutation testing, receptors/pathways must be given from .txt file.")
        if hasattr(self, "custom_lig_path") and self.mod_type.isin(["ligand", "lr"]):
            kwargs["custom_lig_path"] = self.custom_ligands_path
        elif hasattr(self, "custom_pathways_path") and self.mod_type.isin(["ligand", "lr"]):
            kwargs["custom_pathways_path"] = self.custom_pathways_path
        else:
            raise ValueError("For permutation testing, ligands/pathways must be given from .txt file.")

        kwargs["targets_path"] = target_path
        kwargs["mod_type"] = self.mod_type
        kwargs["distance_secreted"] = self.distance_secreted
        kwargs["distance_membrane_bound"] = self.distance_membrane_bound
        kwargs["bw_fixed"] = self.bw_fixed
        kwargs["kernel"] = self.kernel

        comm, parser, args_list = define_spateo_argparse(**kwargs)
        permutation_model = MuSIC(comm, parser, args_list)
        permutation_model._set_up_model()
        permutation_model.fit()
        permutation_model.predict_and_save()

    def eval_permutation_test(self, gene: str):
        """Evaluation function for permutation tests. Will compute multiple metrics (correlation coefficients,
        F1 scores, AUROC in the case that all cells were permuted, etc.) to compare true and model-predicted gene
        expression vectors.

        Args:
            gene: Target gene for which to evaluate permutation test
        """

        parent_dir = os.path.dirname(self.adata_path)
        file_name = os.path.basename(self.adata_path).split(".")[0]

        output_dir = os.path.join(parent_dir, f"permutation_test_outputs_{gene}")
        if not os.path.exists(os.path.join(output_dir, "diagnostics")):
            os.makedirs(os.path.join(output_dir, "diagnostics"))

        if self.permuted_nonzeros_only:
            adata_permuted = anndata.read_h5ad(
                os.path.join(parent_dir, "permutation_test", f"{file_name}_{gene}_permuted_expressing_subset.h5ad")
            )
        else:
            adata_permuted = anndata.read_h5ad(
                os.path.join(parent_dir, "permutation_test", f"{file_name}_{gene}_permuted.h5ad")
            )

        predictions = pd.read_csv(os.path.join(output_dir, "predictions.csv"), index_col=0)
        original_column_names = predictions.columns.tolist()

        # Create a dictionary to map integer column names to new permutation names
        column_name_map = {}
        for column_name in original_column_names:
            if column_name != "nonpermuted":
                column_name_map[column_name] = f"permutation_{column_name}"

        # Rename the columns in the dataframe using the created dictionary
        predictions.rename(columns=column_name_map, inplace=True)

        if not self.permuted_nonzeros_only:
            # Instantiate metric storage variables:
            all_pearson_correlations = {}
            all_spearman_correlations = {}
            all_f1_scores = {}
            all_auroc_scores = {}
            all_rmse_scores = {}

        # For the nonzero subset- will be used both in the case that permutation occurred across all cells and
        # across only gene-expressing cells:
        all_pearson_correlations_nz = {}
        all_spearman_correlations_nz = {}
        all_f1_scores_nz = {}
        all_auroc_scores_nz = {}
        all_rmse_scores_nz = {}

        for col in predictions.columns:
            if "_" in col:
                perm_no = col.split("_")[1]
                y = adata_permuted[:, f"{gene}_permuted_{perm_no}"].X.toarray().reshape(-1)
            else:
                y = adata_permuted[:, f"{gene}_{col}"].X.toarray().reshape(-1)
            y_binary = (y > 0).astype(int)

            y_pred = predictions[col].values.reshape(-1)
            y_pred_binary = (y_pred > 0).astype(int)

            # Compute metrics for the subset of rows that are nonzero:
            nonzero_indices = np.nonzero(y)[0]
            y_nonzero = y[nonzero_indices]
            y_pred_nonzero = y_pred[nonzero_indices]

            y_binary_nonzero = y_binary[nonzero_indices]
            y_pred_binary_nonzero = y_pred_binary[nonzero_indices]

            rp, _ = pearsonr(y_nonzero, y_pred_nonzero)
            r, _ = spearmanr(y_nonzero, y_pred_nonzero)
            f1 = f1_score(y_binary_nonzero, y_pred_binary_nonzero)
            auroc = roc_auc_score(y_binary_nonzero, y_pred_binary_nonzero)
            rmse = mean_squared_error(y_nonzero, y_pred_nonzero, squared=False)

            all_pearson_correlations_nz[col] = rp
            all_spearman_correlations_nz[col] = r
            all_f1_scores_nz[col] = f1
            all_auroc_scores_nz[col] = auroc
            all_rmse_scores_nz[col] = rmse

            # Additionally calculate metrics for all cells if permutation occurred over all cells:
            if not self.permuted_nonzeros_only:
                rp, _ = pearsonr(y, y_pred)
                r, _ = spearmanr(y, y_pred)
                f1 = f1_score(y_binary, y_pred_binary)
                auroc = roc_auc_score(y_binary, y_pred_binary)
                rmse = mean_squared_error(y, y_pred, squared=False)

                all_pearson_correlations[col] = rp
                all_spearman_correlations[col] = r
                all_f1_scores[col] = f1
                all_auroc_scores[col] = auroc
                all_rmse_scores[col] = rmse

        # Collect all diagnostics in dataframe form:
        if not self.permuted_nonzeros_only:
            results = pd.DataFrame(
                {
                    "Pearson correlation": all_pearson_correlations,
                    "Spearman correlation": all_spearman_correlations,
                    "F1 score": all_f1_scores,
                    "AUROC": all_auroc_scores,
                    "RMSE": all_rmse_scores,
                    "Pearson correlation (expressing subset)": all_pearson_correlations_nz,
                    "Spearman correlation (expressing subset)": all_spearman_correlations_nz,
                    "F1 score (expressing subset)": all_f1_scores_nz,
                    "AUROC (expressing subset)": all_auroc_scores_nz,
                    "RMSE (expressing subset)": all_rmse_scores_nz,
                }
            )
            # Without nonpermuted scores:
            results_permuted = results.loc[[r for r in results.index if r != "nonpermuted"], :]

            self.logger.info("Average permutation metrics for all cells: ")
            self.logger.info(f"Average Pearson correlation: {results_permuted['Pearson correlation'].mean()}")
            self.logger.info(f"Average Spearman correlation: {results_permuted['Spearman correlation'].mean()}")
            self.logger.info(f"Average F1 score: {results_permuted['F1 score'].mean()}")
            self.logger.info(f"Average AUROC: {results_permuted['AUROC'].mean()}")
            self.logger.info(f"Average RMSE: {results_permuted['RMSE'].mean()}")
            self.logger.info("Average permutation metrics for expressing cells: ")
            self.logger.info(
                f"Average Pearson correlation: " f"{results_permuted['Pearson correlation (expressing subset)'].mean()}"
            )
            self.logger.info(
                f"Average Spearman correlation: "
                f"{results_permuted['Spearman correlation (expressing subset)'].mean()}"
            )
            self.logger.info(f"Average F1 score: {results_permuted['F1 score (expressing subset)'].mean()}")
            self.logger.info(f"Average AUROC: {results_permuted['AUROC (expressing subset)'].mean()}")
            self.logger.info(f"Average RMSE: {results_permuted['RMSE (expressing subset)'].mean()}")

            diagnostic_path = os.path.join(output_dir, "diagnostics", f"{gene}_permutations_diagnostics.csv")
        else:
            results = pd.DataFrame(
                {
                    "Pearson correlation (expressing subset)": all_pearson_correlations_nz,
                    "Spearman correlation (expressing subset)": all_spearman_correlations_nz,
                    "F1 score (expressing subset)": all_f1_scores_nz,
                    "AUROC (expressing subset)": all_auroc_scores_nz,
                    "RMSE (expressing subset)": all_rmse_scores_nz,
                }
            )
            # Without nonpermuted scores:
            results_permuted = results.loc[[r for r in results.index if r != "nonpermuted"], :]

            self.logger.info("Average permutation metrics for expressing cells: ")
            self.logger.info(
                f"Average Pearson correlation: " f"{results_permuted['Pearson correlation (expressing subset)'].mean()}"
            )
            self.logger.info(
                f"Average Spearman correlation: "
                f"{results_permuted['Spearman correlation (expressing subset)'].mean()}"
            )
            self.logger.info(f"Average F1 score: {results_permuted['F1 score (expressing subset)'].mean()}")
            self.logger.info(f"Average AUROC: {results_permuted['AUROC (expressing subset)'].mean()}")
            self.logger.info(f"Average RMSE: {results_permuted['RMSE (expressing subset)'].mean()}")

            diagnostic_path = os.path.join(
                output_dir, "diagnostics", f"{gene}_nonzero_subset_permutations_diagnostics.csv"
            )

        # Significance testing:
        nonpermuted_values = results.loc["nonpermuted"]

        # Create dictionaries to store the t-statistics, p-values, and significance indicators:
        t_statistics, pvals, significance = {}, {}, {}

        # Iterate over the columns of the DataFrame
        for col in results_permuted.columns:
            column_data = results_permuted[col]
            # Perform one-sample t-test:
            t_stat, pval = ttest_1samp(column_data, nonpermuted_values[col])
            # Store the t-statistic, p-value, and significance indicator
            t_statistics[col] = t_stat
            pvals[col] = pval
            significance[col] = "yes" if pval < 0.05 else "no"

        # Store the t-statistics, p-values, and significance indicators in the results DataFrame:
        results.loc["t-statistic"] = t_statistics
        results.loc["p-value"] = pvals
        results.loc["significant"] = significance

        # Save results:
        results.to_csv(diagnostic_path)

    # ---------------------------------------------------------------------------------------------------
    # In silico perturbation of signaling effects
    # ---------------------------------------------------------------------------------------------------
    def predict_perturbation_effect(
        self,
        ligand: Optional[str] = None,
        receptor: Optional[str] = None,
        regulator: Optional[str] = None,
        cell_type: Optional[str] = None,
    ):
        """Basic & theoretical in silico perturbation, to depict the effect of changing expression level of a given
        signaling molecule or upstream regulator. In silico perturbation will set the level of the specified
        regulator to 0.

        Args:
            ligand: Expression of this ligand will be set to 0
            receptor: Expression of this receptor will be set to 0
            regulator: Expression of this regulator will be set to 0. Examples of "regulators" are transcription
                factors or cofactors, anything that comprises the independent variable array found by :func `
            cell_type:

        Returns:

        """
        # For ligand or L:R models, recompute neighborhood ligand level if perturbing a ligand:
        if ligand is not None and self.mod_type in ["ligand", "lr"]:
            "filler"

    # ---------------------------------------------------------------------------------------------------
    # Cell type coupling:
    # ---------------------------------------------------------------------------------------------------
    def compute_cell_type_coupling(
        self,
        targets: Optional[Union[str, List[str]]] = None,
        effect_strength_threshold: Optional[float] = None,
    ):
        """Generates heatmap of spatially differentially-expressed features for each pair of sender and receiver
        categories- if :attr `mod_type` is "niche", this directly averages the effects for each neighboring cell type
        for each observation. If :attr `mod_type` is "lr" or "ligand", this correlates cell type prevalence with the
        size of the predicted effect on downstream expression for each L:R pair.

        Args:
            targets: Optional string or list of strings to select targets from among the genes used to fit the model
                to compute signaling effects for. If not given, will use all targets.
            effect_strength_threshold: Optional percentile for filtering the computed signaling effect. If not None,
                will filter to those cells for which a given signaling effect is predicted to have a strong effect
                on target gene expression. Otherwise, will compute cell type coupling over all cells in the sample.

        Returns:
            ct_coupling: 3D array summarizing cell type coupling in terms of effect on downstream expression
            ct_coupling_significance: 3D array summarizing significance of cell type coupling in terms of effect on
                downstream expression
        """

        if effect_strength_threshold is not None:
            effect_strength_threshold = 0.2
            self.logger.info(
                f"Computing cell type coupling for cells in which predicted sent/received effect score "
                f"is higher than {effect_strength_threshold * 100}th percentile score."
            )

        if not self.mod_type != "receptor":
            raise ValueError("Knowledge of the source is required to sent effect potential.")

        if self.mod_type in ["lr", "ligand"]:
            # Get spatial weights given bandwidth value- each row corresponds to a sender, each column to a receiver:
            # Try to load spatial weights for membrane-bound and secreted ligands, compute if not found:
            membrane_bound_path = os.path.join(
                os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_membrane_bound.npz"
            )
            secreted_path = os.path.join(
                os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_secreted.npz"
            )

            try:
                spatial_weights_membrane_bound = scipy.sparse.load_npz(membrane_bound_path)
                spatial_weights_secreted = scipy.sparse.load_npz(secreted_path)
            except:
                bw_mb = (
                    self.n_neighbors_membrane_bound
                    if self.distance_membrane_bound is None
                    else self.distance_membrane_bound
                )
                bw_fixed = True if self.distance_membrane_bound is not None else False
                spatial_weights_membrane_bound = self._compute_all_wi(
                    bw=bw_mb,
                    bw_fixed=bw_fixed,
                    exclude_self=True,
                    verbose=False,
                )
                self.logger.info(f"Saving spatial weights for membrane-bound ligands to {membrane_bound_path}.")
                scipy.sparse.save_npz(membrane_bound_path, spatial_weights_membrane_bound)

                bw_s = self.n_neighbors_membrane_bound if self.distance_secreted is None else self.distance_secreted
                bw_fixed = True if self.distance_secreted is not None else False
                # Autocrine signaling is much easier with secreted signals:
                spatial_weights_secreted = self._compute_all_wi(
                    bw=bw_s,
                    bw_fixed=bw_fixed,
                    exclude_self=False,
                    verbose=False,
                )
                self.logger.info(f"Saving spatial weights for secreted ligands to {secreted_path}.")
                scipy.sparse.save_npz(secreted_path, spatial_weights_secreted)
        else:
            niche_path = os.path.join(
                os.path.splitext(self.output_path)[0], "spatial_weights", "spatial_weights_niche.npz"
            )

            try:
                spatial_weights_niche = scipy.sparse.load_npz(niche_path)
            except:
                spatial_weights_niche = self._compute_all_wi(
                    bw=self.n_neighbors_niche, bw_fixed=False, exclude_self=False, kernel="bisquare"
                )
                self.logger.info(f"Saving spatial weights for niche to {niche_path}.")
                scipy.sparse.save_npz(niche_path, spatial_weights_niche)

        # Compute signaling potential for each target (mediated by each of the possible signaling patterns-
        # ligand/receptor or cell type/cell type pair):
        # Columns consist of the spatial weights of each observation- convolve with expression of each ligand to
        # get proxy of ligand signal "sent", weight by the local coefficient value to get a proxy of the "signal
        # functionally received" in generating the downstream effect and store in .obsp.
        if targets is None:
            targets = self.coeffs.keys()
        elif isinstance(targets, str):
            targets = [targets]

        # Can optionally restrict to targets that are well-predicted by the model
        if self.filter_targets:
            pearson_dict = {}
            for target in targets:
                observed = self.adata[:, target].X.toarray().reshape(-1, 1)
                predicted = self.predictions[target].reshape(-1, 1)

                # Remove index of the largest predicted value (to mitigate sensitivity of these metrics to outliers):
                outlier_index = np.where(np.max(predicted))[0]
                predicted = np.delete(predicted, outlier_index)
                observed = np.delete(observed, outlier_index)

                rp, _ = pearsonr(observed, predicted)
                pearson_dict[target] = rp

            targets = [target for target in targets if pearson_dict[target] > self.filter_target_threshold]

        # Cell type pairings:
        if not hasattr(self, "cell_categories"):
            group_name = self.adata.obs[self.group_key]
            # db = pd.DataFrame({"group": group_name})
            db = pd.DataFrame({"group": group_name})
            categories = np.array(group_name.unique().tolist())
            # db["group"] = pd.Categorical(db["group"], categories=categories)
            db["group"] = pd.Categorical(db["group"], categories=categories)

            self.logger.info("Preparing data: converting categories to one-hot labels for all samples.")
            X = pd.get_dummies(data=db, drop_first=False)
            # Ensure columns are in order:
            self.cell_categories = X.reindex(sorted(X.columns), axis=1)
            # Ensure each category is one word with no spaces or special characters:
            self.cell_categories.columns = [
                re.sub(r"\b([a-zA-Z0-9])", lambda match: match.group(1).upper(), re.sub(r"[^a-zA-Z0-9]+", "", s))
                for s in self.cell_categories.columns
            ]

        celltype_pairs = list(itertools.product(self.cell_categories.columns, self.cell_categories.columns))
        celltype_pairs = [f"{cat[0]}-{cat[1]}" for cat in celltype_pairs]

        if self.mod_type in ["lr", "ligand"]:
            cols = self.lr_pairs if self.mod_type == "lr" else self.ligands
        else:
            cols = celltype_pairs

        # Storage for cell type-cell type coupling results- primary axis: targets, secondary: L:R pairs/ligands,
        # tertiary: cell type pairs:
        ct_coupling = np.zeros((len(targets), len(cols), len(celltype_pairs)))
        ct_coupling_significance = np.zeros((len(targets), len(cols), len(celltype_pairs)))

        for i, target in enumerate(targets):
            for j, col in enumerate(cols):
                if self.mod_type == "lr":
                    ligand = col[0]
                    receptor = col[1]

                    effect_potential, _, _ = self.get_effect_potential(
                        target=target,
                        ligand=ligand,
                        receptor=receptor,
                        spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        spatial_weights_secreted=spatial_weights_secreted,
                    )

                    # For each cell type pair, compute average effect potential across all cells of the sending and
                    # receiving type for those cells that are sending + receiving signal:
                    for k, pair in enumerate(celltype_pairs):
                        sending_cell_type = pair.split("-")[0]
                        receiving_cell_type = pair.split("-")[1]

                        # Get indices of cells of each type:
                        sending_indices = np.where(self.cell_categories[sending_cell_type] == 1)[0]
                        receiving_indices = np.where(self.cell_categories[receiving_cell_type] == 1)[0]

                        # Get average effect potential across all cells of each type- first filter if threshold is
                        # given:
                        if effect_strength_threshold is not None:
                            effect_potential_data = effect_potential.data
                            # Threshold is taken to be a percentile value:
                            effect_strength_threshold = np.percentile(
                                effect_potential_data, effect_strength_threshold * 100
                            )
                            strong_effect_mask = effect_potential > effect_strength_threshold
                            rem_row_indices, rem_col_indices = strong_effect_mask.nonzero()

                            # Update sending and receiving indices to now include cells of the given sending and
                            # receiving type that send/receive signal:
                            sending_indices = np.intersect1d(sending_indices, rem_row_indices)
                            receiving_indices = np.intersect1d(receiving_indices, rem_col_indices)

                        # Check if there is no signal being transmitted and/or received between cells of the given
                        # two types:
                        if len(sending_indices) == 0 or len(receiving_indices) == 0:
                            ct_coupling[i, j, k] = 0
                            ct_coupling_significance[i, j, k] = 0
                            continue

                        avg_effect_potential = np.mean(effect_potential[sending_indices, receiving_indices])
                        ct_coupling[i, j, k] = avg_effect_potential
                        ct_coupling_significance[i, j, k] = permutation_testing(
                            effect_potential,
                            n_permutations=10000,
                            n_jobs=30,
                            subset_rows=sending_indices,
                            subset_cols=receiving_indices,
                        )

                elif self.mod_type == "ligand":
                    effect_potential, _, _ = self.get_effect_potential(
                        target=target,
                        ligand=col,
                        spatial_weights_membrane_bound=spatial_weights_membrane_bound,
                        spatial_weights_secreted=spatial_weights_secreted,
                    )

                    # For each cell type pair, compute average effect potential across all cells of the sending and
                    # receiving type:
                    for k, pair in enumerate(celltype_pairs):
                        sending_cell_type = pair.split("-")[0]
                        receiving_cell_type = pair.split("-")[1]

                        # Get indices of cells of each type:
                        sending_indices = np.where(self.cell_categories[sending_cell_type] == 1)[0]
                        receiving_indices = np.where(self.cell_categories[receiving_cell_type] == 1)[0]

                        # Get average effect potential across all cells of each type:
                        avg_effect_potential = np.mean(effect_potential[sending_indices, receiving_indices])
                        ct_coupling[i, j, k] = avg_effect_potential
                        ct_coupling_significance[i, j, k] = permutation_testing(
                            effect_potential,
                            n_permutations=10000,
                            n_jobs=30,
                            subset_rows=sending_indices,
                            subset_cols=receiving_indices,
                        )

                elif self.mod_type == "niche":
                    sending_cell_type = col.split("-")[0]
                    receiving_cell_type = col.split("-")[1]
                    effect_potential, _, _ = self.get_effect_potential(
                        target=target,
                        sender_cell_type=sending_cell_type,
                        receiver_cell_type=receiving_cell_type,
                        spatial_weights_niche=spatial_weights_niche,
                    )

                    # Directly compute the average- the processing steps when providing sender and receiver cell
                    # types already handle filtering down to the pertinent cells- but for the permutation we still have
                    # to supply indices to keep track of the original indices of the cells:
                    for k, pair in enumerate(celltype_pairs):
                        sending_cell_type = pair.split("-")[0]
                        receiving_cell_type = pair.split("-")[1]

                        # Get indices of cells of each type:
                        sending_indices = np.where(self.cell_categories[sending_cell_type] == 1)[0]
                        receiving_indices = np.where(self.cell_categories[receiving_cell_type] == 1)[0]

                        avg_effect_potential = np.mean(effect_potential)
                        ct_coupling[i, j, k] = avg_effect_potential
                        ct_coupling_significance[i, j, k] = permutation_testing(
                            avg_effect_potential,
                            n_permutations=10000,
                            n_jobs=30,
                            subset_rows=sending_indices,
                            subset_cols=receiving_indices,
                        )

        # Save results:
        parent_dir = os.path.dirname(self.output_path)
        if not os.path.exists(os.path.join(parent_dir, "cell_type_coupling")):
            os.makedirs(os.path.join(parent_dir, "cell_type_coupling"))

        # Convert Numpy array to xarray object for storage and save as .h5 object:
        ct_coupling = xr.DataArray(
            ct_coupling,
            dims=["target", "signal_source", "celltype_pair"],
            coords={"target": targets, "signal_source": cols, "celltype_pair": celltype_pairs},
            name="ct_coupling",
        )

        ct_coupling_significance = xr.DataArray(
            ct_coupling_significance,
            dims=["target", "signal_source", "celltype_pair"],
            coords={"target": targets, "signal_source": cols, "celltype_pair": celltype_pairs},
            name="ct_coupling_significance",
        )
        coupling_results_path = os.path.join(
            parent_dir, "cell_type_coupling", "celltype_effects_coupling_and_significance.nc"
        )

        # Combine coupling and significance into the same dataset:
        ds = xr.merge([ct_coupling, ct_coupling_significance])
        ds.to_netcdf(coupling_results_path)

        return ct_coupling, ct_coupling_significance

    def pathway_coupling(self, pathway: Union[str, List[str]]):
        """From computed cell type coupling results, compute pathway coupling by leveraging the pathway membership of
        constituent ligands/ligand:receptor pairs.

        Args:
            pathway: Name of the pathway(s) to compute coupling for.

        Returns:
            pathway_coupling: Dictionary where pathway names are indices and values are coupling score dataframes for
                the pathway
            pathway_coupling_significance: Dictionary where pathway names are indices and values are coupling score
                significance dataframes for the pathway
        """
        # Check for already existing cell coupling results:
        parent_dir = os.path.dirname(self.output_path)
        coupling_results_path = os.path.join(
            parent_dir, "cell_type_coupling", "celltype_effects_coupling_and_significance.nc"
        )

        try:
            coupling_ds = xr.open_dataset(coupling_results_path)
            coupling_results = coupling_ds["ct_coupling"]
            coupling_significance = coupling_ds["ct_coupling_significance"]
        except FileNotFoundError:
            self.logger.info("No coupling results found. Computing cell type coupling...")
            coupling_results, coupling_significance = self.compute_cell_type_coupling()

        predictors = list(coupling_results["signal_source"].values)

        # For chosen pathway(s), get the ligands/ligand:receptor pairs that are members:
        if isinstance(pathway, str):
            if self.mod_type == "lr":
                pathway_ligands = list(self.lr_db.loc[self.lr_db["pathway"] == pathway, "from"].values)
                pathway_receptors = list(self.lr_db.loc[self.lr_db["pathway"] == pathway, "to"].values)
                all_pathway_lr = [f"{l}:{r}" for l, r in zip(pathway_ligands, pathway_receptors)]
                matched_pathway_lr = list(set(all_pathway_lr).intersection(set(predictors)))
                # Make sure the pathway has at least three ligands or ligand-receptor pairs after processing-
                # otherwise, there is not enough measured signal in the model to constitute a pathway:
                if len(matched_pathway_lr) < 3:
                    raise ValueError(
                        "The chosen pathway has too little representation (<= 3 interactions) in the modeling "
                        "features. Specify a different pathway or fit an additional model."
                    )

                matched_pathway_coupling_scores = [
                    coupling_results.sel(signal_source=lr).values for lr in matched_pathway_lr
                ]
                matched_pathway_coupling_significance = [
                    coupling_significance.sel(signal_source=lr).values for lr in matched_pathway_lr
                ]

            elif self.mod_type == "ligand":
                pathway_ligands = list(self.lr_db.loc[self.lr_db["pathway"] == pathway, "from"].values)
                matched_pathway_ligands = list(set(pathway_ligands).intersection(set(predictors)))
                # Make sure the pathway has at least three ligands or ligand-receptor pairs after processing-
                # otherwise, there is not enough measured signal in the model to constitute a pathway:
                if len(matched_pathway_ligands) < 3:
                    raise ValueError(
                        "The chosen pathway has too little representation (<= 3 interactions) in the modeling "
                        "features. Specify a different pathway or fit an additional model."
                    )

                matched_pathway_coupling_scores = [
                    coupling_results.sel(signal_source=ligand).values for ligand in matched_pathway_ligands
                ]
                matched_pathway_coupling_significance = [
                    coupling_significance.sel(signal_source=ligand).values for ligand in matched_pathway_ligands
                ]

            # Compute mean over pathway:
            stack = np.hstack(matched_pathway_coupling_scores)
            pathway_coupling = np.mean(stack, axis=0)

            # Convert to DataFrame:
            pathway_coupling_df = pd.DataFrame(
                pathway_coupling,
                index=list(coupling_results["target"].values),
                columns=list(coupling_results["celltype_pair"].values),
            )

            # And pathway score significance- if the majority of pathway L:R pairs are significant, then consider
            # the pathway significant for the given cell type pair + target combo:
            stack = np.hstack(matched_pathway_coupling_significance)
            pathway_coupling_significance = np.mean(stack, axis=0)
            pathway_coupling_significance[pathway_coupling_significance >= 0.5] = True

            # Convert to DataFrame:
            pathway_coupling_significance_df = pd.DataFrame(
                pathway_coupling_significance,
                index=list(coupling_results["target"].values),
                columns=list(coupling_results["celltype_pair"].values),
            )

            # Store in dictionary:
            pathway_coupling = {pathway: pathway_coupling_df}
            pathway_coupling_significance = {pathway: pathway_coupling_significance_df}

        elif isinstance(pathway, list):
            pathway_coupling = {}
            pathway_coupling_significance = {}

            for p in pathway:
                if self.mod_type == "lr":
                    pathway_ligands = list(self.lr_db.loc[self.lr_db["pathway"] == p, "from"].values)
                    pathway_receptors = list(self.lr_db.loc[self.lr_db["pathway"] == p, "to"].values)
                    all_pathway_lr = [f"{l}:{r}" for l, r in zip(pathway_ligands, pathway_receptors)]
                    matched_pathway_lr = list(set(all_pathway_lr).intersection(set(predictors)))
                    # Make sure the pathway has at least three ligands or ligand-receptor pairs after processing-
                    # otherwise, there is not enough measured signal in the model to constitute a pathway:
                    if len(matched_pathway_lr) < 3:
                        raise ValueError(
                            "The chosen pathway has too little representation (<= 3 interactions) in the modeling "
                            "features. Specify a different pathway or fit an additional model."
                        )

                    matched_pathway_coupling_scores = [
                        coupling_results.sel(signal_source=lr).values for lr in matched_pathway_lr
                    ]
                    matched_pathway_coupling_significance = [
                        coupling_significance.sel(signal_source=lr).values for lr in matched_pathway_lr
                    ]

                elif self.mod_type == "ligand":
                    pathway_ligands = list(self.lr_db.loc[self.lr_db["pathway"] == p, "from"].values)
                    matched_pathway_ligands = list(set(pathway_ligands).intersection(set(predictors)))
                    # Make sure the pathway has at least three ligands or ligand-receptor pairs after processing-
                    # otherwise, there is not enough measured signal in the model to constitute a pathway:
                    if len(matched_pathway_ligands) < 3:
                        raise ValueError(
                            "The chosen pathway has too little representation (<= 3 interactions) in the modeling "
                            "features. Specify a different pathway or fit an additional model."
                        )

                    matched_pathway_coupling_scores = [
                        coupling_results.sel(signal_source=ligand).values for ligand in matched_pathway_ligands
                    ]
                    matched_pathway_coupling_significance = [
                        coupling_significance.sel(signal_source=ligand).values for ligand in matched_pathway_ligands
                    ]

                # Compute mean over pathway:
                stack = np.hstack(matched_pathway_coupling_scores)
                pathway_coupling = np.mean(stack, axis=0)

                # Convert to DataFrame:
                pathway_coupling_df = pd.DataFrame(
                    pathway_coupling,
                    index=list(coupling_results["target"].values),
                    columns=list(coupling_results["celltype_pair"].values),
                )

                # And pathway score significance- if the majority of pathway L:R pairs are significant, then consider
                # the pathway significant for the given cell type pair + target combo:
                stack = np.hstack(matched_pathway_coupling_significance)
                pathway_coupling_significance = np.mean(stack, axis=0)
                pathway_coupling_significance[pathway_coupling_significance >= 0.5] = True

                # Convert to DataFrame:
                pathway_coupling_significance_df = pd.DataFrame(
                    pathway_coupling_significance,
                    index=list(coupling_results["target"].values),
                    columns=list(coupling_results["celltype_pair"].values),
                )

                # Store in dictionary:
                pathway_coupling[pathway] = pathway_coupling_df
                pathway_coupling_significance[pathway] = pathway_coupling_significance_df

        return pathway_coupling, pathway_coupling_significance


# ---------------------------------------------------------------------------------------------------
# Formatting functions
# ---------------------------------------------------------------------------------------------------
def replace_col_with_collagens(string):
    # Split the string at the colon (if any)
    parts = string.split(":")
    # Split the first part of the string at slashes
    elements = parts[0].split("/")
    # Flag to check if we've encountered a "COL" element or a "Collagens" element
    encountered_col = False

    # Process each element
    for i, element in enumerate(elements):
        # If the element starts with "COL" or "b_COL", or if it is "Collagens" or "b_Collagens"
        if element.startswith("COL") or element.startswith("b_COL") or element in ["Collagens", "b_Collagens"]:
            # If we've already encountered a "COL" or "Collagens" element, remove this one
            if encountered_col:
                elements[i] = None
            # Otherwise, replace it with "Collagens" or "b_Collagens" as appropriate
            else:
                if element.startswith("b_COL") or element == "b_Collagens":
                    elements[i] = "b_Collagens"
                else:
                    elements[i] = "Collagens"
                encountered_col = True

    # Remove None elements and join the rest with slashes
    replaced_part = "/".join([element for element in elements if element is not None])
    # If there's a second part, add it back
    if len(parts) > 1:
        replaced_string = replaced_part + ":" + parts[1]
    else:
        replaced_string = replaced_part

    return replaced_string


def replace_hla_with_hlas(string):
    # Split the string at the colon (if any)
    parts = string.split(":")
    # Split the first part of the string at slashes
    elements = parts[0].split("/")
    # Flag to check if we've encountered an "HLA" element or an "HLAs" element
    encountered_hla = False

    # Process each element
    for i, element in enumerate(elements):
        # If the element starts with "HLA" or "b_HLA", or if it is "HLAs" or "b_HLAs"
        if element.startswith("HLA") or element.startswith("b_HLA") or element in ["HLAs", "b_HLAs"]:
            # If we've already encountered an "HLA" or "HLAs" element, remove this one
            if encountered_hla:
                elements[i] = None
            # Otherwise, replace it with "HLAs" or "b_HLAs" as appropriate
            else:
                if element.startswith("b_HLA") or element == "b_HLAs":
                    elements[i] = "b_HLAs"
                else:
                    elements[i] = "HLAs"
                encountered_hla = True

    # Remove None elements and join the rest with slashes
    replaced_part = "/".join([element for element in elements if element is not None])
    # If there's a second part, add it back
    if len(parts) > 1:
        replaced_string = replaced_part + ":" + parts[1]
    else:
        replaced_string = replaced_part

    return replaced_string