"""
Regression function that is considerate of the spatial heterogeneity of (and thus the context-dependency of the
relationships of) the response variable.
"""
import argparse
import copy
import os
import sys
from functools import partial
from multiprocessing import Pool
from typing import Optional, Union

import anndata
import numpy as np
import numpy.linalg
import pandas as pd
import scipy
from mpi4py import MPI

# For now, add Spateo working directory to sys path so compiler doesn't look in the installed packages:
sys.path.insert(0, "/mnt/c/Users/danie/Desktop/Github/Github/spateo-release-main")

from spateo.logging import logger_manager as lm
from spateo.preprocessing.normalize import normalize_total
from spateo.preprocessing.transform import log1p
from spateo.tools.find_neighbors import get_wi, transcriptomic_connectivity
from spateo.tools.spatial_degs import moran_i
from spateo.tools.spatial_smooth.smooth import calc_1nd_moment
from spateo.tools.ST_regression.regression_utils import compute_betas_local, iwls

# NOTE: set lower bound AND upper bound bandwidth much lower for membrane-bound ligands/receptors pairs

# ---------------------------------------------------------------------------------------------------
# GWR
# ---------------------------------------------------------------------------------------------------
class STGWR:
    """Geographically weighted regression on spatial omics data with parallel processing. Runs after being called
    from the command line.

    Args:
        MPI_comm: MPI communicator object initialized with mpi4py, to control parallel processing operations
        parser: ArgumentParser object initialized with argparse, to parse command line arguments for arguments
            pertinent to modeling.

    Attributes:
        mod_type: The type of model that will be employed- this dictates how the data will be processed and
            prepared. Options:
                - "niche": Spatially-aware, uses spatial connections between samples as independent variables
                - "lr": Spatially-aware, uses the combination of receptor expression in the "target" cell and spatially
                    lagged ligand expression in the neighboring cells as independent variables.
                - "slice": Spatially-aware, uses a coupling of spatial category connections, ligand expression
                    and receptor expression to perform regression on select receptor-downstream genes.


        data_path: Path to the AnnData object from which to extract data for modeling
        normalize: Set True to Perform library size normalization, to set total counts in each cell to the same
            number (adjust for cell size). It is advisable not to do this if performing Poisson or negative binomial
            regression.
        smooth: Set True to correct for dropout effects by leveraging gene expression neighborhoods to smooth
            expression. It is advisable not to do this if performing Poisson or negative binomial regression.
        log_transform: Set True if log-transformation should be applied to expression. It is advisable not to do
            this if performing Poisson or negative binomial regression.


        custom_lig_path: Only used if :param `mod_type` is "lr" or "slice". Optional path to a .txt file containing a
            list of ligands for the model, separated by newlines. Only used if :attr `mod_type` is "lr" or "slice" (
            and thus uses ligand/receptor expression directly in the inference). If not provided, will select
            ligands using a threshold based on expression levels in the data.
        custom_rec_path: Only used if :param `mod_type` is "lr" or "slice". Optional path to a .txt file containing a
            list of receptors for the model, separated by newlines. Only used if :attr `mod_type` is "lr" or "slice"
            (and thus uses ligand/receptor expression directly in the inference). If not provided, will select
            receptors using a threshold based on expression levels in the data.
        targets_path: Optional path to a .txt file containing a list of prediction target genes for the model,
            separated by newlines. If not provided, targets will be strategically selected from the given receptors.


        cci_dir: Full path to the directory containing cell-cell communication databases
        species: Selects the cell-cell communication database the relevant ligands will be drawn from. Options:
                "human", "mouse".
        output_path: Full path name for the .csv file in which results will be saved


        coords_key: Key in .obsm of the AnnData object that contains the coordinates of the cells
        group_key: Key in .obs of the AnnData object that contains the category grouping for each cell


        bw: Used to provide previously obtained bandwidth for the spatial kernel. Consists of either a distance
            value or N for the number of nearest neighbors. Can be obtained using BW_Selector or some other
            user-defined method. Pass "np.inf" if all other points should have the same spatial weight. Defaults to
            1000 if not provided.
        minbw: For use in automated bandwidth selection- the lower-bound bandwidth to test.
        maxbw: For use in automated bandwidth selection- the upper-bound bandwidth to test.


        distr: Distribution family for the dependent variable; one of "gaussian", "poisson", "nb"
        kernel: Type of kernel function used to weight observations; one of "bisquare", "exponential", "gaussian",
            "quadratic", "triangular" or "uniform".


        bw_fixed: Set True for distance-based kernel function and False for nearest neighbor-based kernel function
        fit_intercept: Set True to include intercept in the model and False to exclude intercept
    """

    def __init__(self, comm: MPI.Comm, parser: argparse.ArgumentParser):
        self.logger = lm.get_main_logger()

        self.comm = comm
        self.parser = parser

        self.mod_type = None
        self.species = None
        self.ligands = None
        self.receptors = None
        self.targets = None
        self.normalize = None
        self.smooth = None
        self.log_transform = None

        self.coords = None
        self.groups = None
        self.y = None
        self.X = None

        self.bw = None
        self.minbw = None
        self.maxbw = None

        self.distr = None
        self.kernel = None
        self.n_samples = None
        self.n_features = None
        self.iterations = None

        self.parse_stgwr_args()

        # Check if the program is currently in the master process:
        if self.comm.rank == 0:
            self.load_and_process()

    def parse_stgwr_args(self):
        """
        Parse command line arguments for arguments pertinent to modeling.
        """
        arg_retrieve = self.parser.parse_args()
        self.mod_type = arg_retrieve.mod_type
        self.adata_path = arg_retrieve.data_path
        self.cci_dir = arg_retrieve.cci_dir
        self.species = arg_retrieve.species
        self.output_path = arg_retrieve.output_path
        self.custom_ligands_path = arg_retrieve.custom_lig_path
        self.custom_receptors_path = arg_retrieve.custom_rec_path
        self.targets_path = arg_retrieve.targets_path

        self.normalize = arg_retrieve.normalize
        self.smooth = arg_retrieve.smooth
        self.log_transform = arg_retrieve.log_transform

        self.coords_key = arg_retrieve.coords_key
        self.group_key = arg_retrieve.group_key

        self.bw_fixed = arg_retrieve.bw_fixed
        self.distr = arg_retrieve.distr
        self.kernel = arg_retrieve.kernel

        self.fit_intercept = arg_retrieve.fit_intercept

        if arg_retrieve.bw:
            if self.bw_fixed:
                self.bw = float(arg_retrieve.bw)
            else:
                self.bw = int(arg_retrieve.bw)

        if arg_retrieve.minbw:
            if self.bw_fixed:
                self.minbw = float(arg_retrieve.minbw)
            else:
                self.minbw = int(arg_retrieve.minbw)

        # Helpful messages at process start:
        if self.comm.rank == 0:
            print("-" * 60, flush=True)
            self.logger.info(f"Running STGWR on {self.comm.size} processes...")
            fixed_or_adaptive = "Fixed " if self.bw_fixed else "Adaptive "
            type = fixed_or_adaptive + self.kernel.capitalize()
            self.logger.info(f"Spatial kernel: {type}")
            self.logger.info(f"Model type: {self.mod_type}")

            self.logger.info(f"Loading AnnData object from: {self.adata_path}")
            self.logger.info(f"Loading cell-cell interaction databases from the following folder: {self.cci_dir}")
            if self.custom_ligands_path is not None:
                self.logger.info(f"Using list of custom ligands from: {self.custom_ligands_path}")
            if self.custom_receptors_path is not None:
                self.logger.info(f"Using list of custom receptors from: {self.custom_receptors_path}")
            if self.targets_path is not None:
                self.logger.info(f"Using list of target genes from: {self.targets_path}")
            self.logger.info(f"Saving results to: {self.output_path}")

    def load_and_process(self):
        """
        Load AnnData object and process it for modeling.
        """
        self.adata = anndata.read_h5ad(self.adata_path)
        self.coords = self.adata.obsm[self.coords_key]
        self.n_samples = self.adata.n_obs
        self.n_features = self.adata.n_vars

        if self.distr in ["poisson", "neg-binomial"]:
            if self.normalize or self.smooth or self.log_transform:
                self.logger.info(
                    f"With a {self.distr} assumption, discrete counts are required for the response variable. "
                    f"Computing normalizations and transforms if applicable, but storing the results in the AnnData "
                    f"object and saving the raw counts for use in model fitting."
                )
                self.adata.layers["raw"] = self.adata.X

        if self.normalize:
            if self.distr == "gaussian":
                self.logger.info("Setting total counts in each cell to 1e4 inplace...")
                normalize_total(self.adata)
            else:
                self.logger.info("Setting total counts in each cell to 1e4, storing in adata.layers['X_norm'].")
                dat = normalize_total(self.adata, inplace=False)
                self.adata.layers["X_norm"] = dat["X"]
                self.adata.obs["norm_factor"] = dat["norm_factor"]
                self.adata.layers["stored_processed"] = dat["X"]

        # Smooth data if 'smooth' is True and log-transform data matrix if 'log_transform' is True:
        if self.smooth:
            if self.distr == "gaussian":
                self.logger.info("Smoothing gene expression inplace...")
                # Compute connectivity matrix if not already existing:
                try:
                    conn = self.adata.obsp["expression_connectivities"]
                except:
                    _, adata = transcriptomic_connectivity(self.adata, n_neighbors_method="ball_tree")
                    conn = adata.obsp["expression_connectivities"]
                adata_smooth_norm, _ = calc_1nd_moment(self.adata.X, conn, normalize_W=True)
                self.adata.layers["smooth"] = adata_smooth_norm

                # Use smoothed layer for downstream processing:
                self.adata.layers["raw"] = self.adata.X
                self.adata.X = self.adata.layers["smooth"]

            else:
                self.logger.info(
                    "Smoothing gene expression inplace and storing in in adata.layers['smooth'] or "
                    "adata.layers['normed_smooth'] if normalization was first performed."
                )
                adata_temp = self.adata.copy()
                # Check if normalized expression is present- if 'distr' is one of the indicated distributions AND
                # 'normalize' is True, AnnData will not have been updated in place, with the normalized array
                # instead being stored in the object.
                try:
                    adata_temp.X = adata_temp.layers["X_norm"]
                    norm = True
                except:
                    norm = False
                    pass

                try:
                    conn = self.adata.obsp["expression_connectivities"]
                except:
                    _, adata = transcriptomic_connectivity(adata_temp, n_neighbors_method="ball_tree")
                    conn = adata.obsp["expression_connectivities"]
                adata_smooth_norm, _ = calc_1nd_moment(adata_temp.X, conn, normalize_W=True)
                if norm:
                    self.adata.layers["norm_smooth"] = adata_smooth_norm
                else:
                    self.adata.layers["smooth"] = adata_smooth_norm
                self.adata.layers["stored_processed"] = adata_smooth_norm

        if self.log_transform:
            if self.distr == "gaussian":
                self.logger.info("Log-transforming expression inplace...")
                log1p(self.adata)
            else:
                self.logger.info(
                    "Log-transforming expression and storing in adata.layers['X_log1p'], "
                    "adata.layers['X_norm_log1p'], adata.layers['X_smooth_log1p'], or adata.layers["
                    "'X_norm_smooth_log1p'], depending on the normalizations and transforms that were "
                    "specified."
                )
                adata_temp = self.adata.copy()
                # Check if normalized expression is present- if 'distr' is one of the indicated distributions AND
                # 'normalize' and/or 'smooth' is True, AnnData will not have been updated in place,
                # with the normalized array instead being stored in the object.
                if "norm_smooth" in adata_temp.layers.keys():
                    layer = "norm_smooth"
                    adata_temp.X = adata_temp.layers["norm_smooth"]
                    norm, smoothed = True, True
                elif "smooth" in adata_temp.layers.keys():
                    layer = "smooth"
                    adata_temp.X = adata_temp.layers["smooth"]
                    norm, smoothed = False, True
                elif "X_norm" in adata_temp.layers.keys():
                    layer = "X_norm"
                    adata_temp.X = adata_temp.layers["X_norm"]
                    norm, smoothed = True, False
                else:
                    layer = None
                    norm, smoothed = False, False

                if layer is not None:
                    log1p(adata_temp.layers[layer])
                else:
                    log1p(adata_temp)

                if norm and smoothed:
                    self.adata.layers["X_norm_smooth_log1p"] = adata_temp.X
                elif norm:
                    self.adata.layers["X_norm_log1p"] = adata_temp.X
                elif smoothed:
                    self.adata.layers["X_smooth_log1p"] = adata_temp.X
                else:
                    self.adata.layers["X_log1p"] = adata_temp.X
                self.adata.layers["stored_processed"] = adata_temp.X

        # Define necessary quantities that will later be used to define the independent variable array- the one-hot
        # cell-type array, the ligand expression array and the receptor expression array:

        # One-hot cell type array (or other category):
        if self.mod_type == "niche" or self.mod_type == "slice":
            group_name = self.adata.obs[self.group_key]
            db = pd.DataFrame({"group": group_name})
            categories = np.array(group_name.unique().tolist())
            db["group"] = pd.Categorical(db["group"], categories=categories)

            self.logger.info("Preparing data: converting categories to one-hot labels for all samples.")
            X = pd.get_dummies(data=db, drop_first=False)
            # Ensure columns are in order:
            self.cell_categories = X.reindex(sorted(X.columns), axis=1)

        # Ligand-receptor expression array
        if self.mod_type == "lr" or self.mod_type == "slice":
            if self.species == "human":
                lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_human.csv"), index_col=0)
            elif self.species == "mouse":
                lr_db = pd.read_csv(os.path.join(self.cci_dir, "lr_db_mouse.csv"), index_col=0)
            else:
                self.logger.error("Invalid species specified. Must be one of 'human' or 'mouse'.")
            database_ligands = set(lr_db["from"])
            database_receptors = set(lr_db["to"])

            if self.custom_ligands_path is not None:
                with open(self.custom_ligands_path, "r") as f:
                    ligands = f.read().splitlines()
                    ligands = [l for l in ligands if l in database_ligands]
                    l_complexes = [elem for elem in ligands if "_" in elem]
                    # Get individual components if any complexes are included in this list:
                    ligands = [l for item in ligands for l in item.split("_")]
            else:
                # List of possible complexes to search through:
                l_complexes = [elem for elem in database_ligands if "_" in elem]
                # And all possible ligand molecules:
                all_ligands = [l for item in database_ligands for l in item.split("_")]

                # Get list of ligands from among the most highly spatially-variable genes, indicative of potentially
                # interesting spatially-enriched signal:
                self.logger.info(
                    "Preparing data: getting list of ligands from among the most highly " "spatially-variable genes."
                )
                m_degs = moran_i(self.adata)
                m_filter_genes = m_degs[m_degs.moran_q_val < 0.05].sort_values(by=["moran_i"], ascending=False).index
                ligands = [g for g in m_filter_genes if g in all_ligands]

                # If no significant spatially-variable ligands are found, use the top 10 most spatially-variable
                # ligands:
                if len(ligands) == 0:
                    self.logger.info(
                        "No significant spatially-variable ligands found. Using top 10 most "
                        "spatially-variable ligands."
                    )
                    m_filter_genes = m_degs.sort_values(by=["moran_i"], ascending=False).index
                    ligands = [g for g in m_filter_genes if g in all_ligands][:10]

                # If any ligands are part of complexes, add all complex components to this list:
                for element in l_complexes:
                    if "_" in element:
                        complex_members = element.split("_")
                        for member in complex_members:
                            if member in ligands:
                                other_members = [m for m in complex_members if m != member]
                                for member in other_members:
                                    ligands.append(member)

                self.logger.info(
                    f"Found {len(ligands)} among significantly spatially-variable genes and associated "
                    f"complex members."
                )

            ligands = [l for l in ligands if l in self.adata.var_names]
            self.ligands_expr = pd.DataFrame(
                self.adata[:, ligands].X.toarray() if scipy.sparse.issparse(self.adata.X) else self.adata[:, ligands].X,
                index=self.adata.obs_names,
                columns=ligands,
            )
            # Combine columns if they are part of a complex- eventually the individual columns should be dropped,
            # but store them in a temporary list to do so later because some may contribute to multiple complexes:
            to_drop = []
            for element in l_complexes:
                parts = element.split("_")
                if all(part in self.ligands_expr.columns for part in parts):
                    # Combine the columns into a new column with the name of the hyphenated element- here we will
                    # compute the geometric mean of the expression values of the complex components:
                    self.ligands_expr[element] = self.ligands_expr[parts].apply(
                        lambda x: x.prod() ** (1 / len(parts)), axis=1
                    )
                    # Mark the individual components for removal if the individual components cannot also be
                    # found as ligands:
                    to_drop.extend([part for part in parts if part not in database_ligands])
                else:
                    # Drop the hyphenated element from the dataframe if all components are not found in the
                    # dataframe columns
                    partial_components = [l for l in ligands if l in parts]
                    to_drop.extend(partial_components)
                    if len(partial_components) > 0:
                        self.logger.info(
                            f"Not all components from the {element} heterocomplex could be found in the " f"dataset."
                        )

            # Drop any possible duplicate ligands alongside any other columns to be dropped:
            to_drop = list(set(to_drop))
            self.ligands_expr.drop(to_drop, axis=1, inplace=True)
            first_occurrences = self.ligands_expr.columns.duplicated(keep="first")
            self.ligands_expr = self.ligands_expr.loc[:, ~first_occurrences]

            if self.custom_receptors_path is not None:
                with open(self.custom_receptors_path, "r") as f:
                    receptors = f.read().splitlines()
                    receptors = [r for r in receptors if r in database_receptors]
                    r_complexes = [elem for elem in receptors if "_" in elem]
                    # Get individual components if any complexes are included in this list:
                    receptors = [r for item in receptors for r in item.split("_")]
            else:
                # List of possible complexes to search through:
                r_complexes = [elem for elem in database_receptors if "_" in elem]
                # And all possible receptor molecules:
                all_receptors = [r for item in database_receptors for r in item.split("_")]

                # Get list of receptors from among the most highly spatially-variable genes, indicative of
                # potentially interesting spatially-enriched signal:
                self.logger.info(
                    "Preparing data: getting list of ligands from among the most highly " "spatially-variable genes."
                )
                m_degs = moran_i(self.adata)
                m_filter_genes = m_degs[m_degs.moran_q_val < 0.05].sort_values(by=["moran_i"], ascending=False).index
                receptors = [g for g in m_filter_genes if g in all_receptors]

                # If no significant spatially-variable receptors are found, use the top 10 most spatially-variable
                # receptors:
                if len(receptors) == 0:
                    self.logger.info(
                        "No significant spatially-variable receptors found. Using top 10 most "
                        "spatially-variable receptors."
                    )
                    m_filter_genes = m_degs.sort_values(by=["moran_i"], ascending=False).index
                    receptors = [g for g in m_filter_genes if g in all_receptors][:10]

                # If any receptors are part of complexes, add all complex components to this list:
                for element in r_complexes:
                    if "_" in element:
                        complex_members = element.split("_")
                        for member in complex_members:
                            if member in receptors:
                                other_members = [m for m in complex_members if m != member]
                                for member in other_members:
                                    receptors.append(member)

                self.logger.info(
                    f"Found {len(receptors)} among significantly spatially-variable genes and associated "
                    f"complex members."
                )

            receptors = [r for r in receptors if r in self.adata.var_names]
            self.receptors_expr = pd.DataFrame(
                self.adata[:, receptors].X.toarray()
                if scipy.sparse.issparse(self.adata.X)
                else self.adata[:, receptors].X,
                index=self.adata.obs_names,
                columns=receptors,
            )

            # Combine columns if they are part of a complex- eventually the individual columns should be dropped,
            # but store them in a temporary list to do so later because some may contribute to multiple complexes:
            to_drop = []
            for element in r_complexes:
                if "_" in element:
                    parts = element.split("_")
                    if all(part in self.receptors_expr.columns for part in parts):
                        # Combine the columns into a new column with the name of the hyphenated element- here we will
                        # compute the geometric mean of the expression values of the complex components:
                        self.receptors_expr[element] = self.receptors_expr[parts].apply(
                            lambda x: x.prod() ** (1 / len(parts)), axis=1
                        )
                        # Mark the individual components for removal if the individual components cannot also be
                        # found as receptors:
                        to_drop.extend([part for part in parts if part not in database_receptors])
                    else:
                        # Drop the hyphenated element from the dataframe if all components are not found in the
                        # dataframe columns
                        partial_components = [r for r in receptors if r in parts]
                        to_drop.extend(partial_components)
                        if len(partial_components) > 0:
                            self.logger.info(
                                f"Not all components from the {element} heterocomplex could be found in the "
                                f"dataset, so this complex was not included."
                            )

            # Drop any possible duplicate ligands alongside any other columns to be dropped:
            to_drop = list(set(to_drop))
            self.receptors_expr.drop(to_drop, axis=1, inplace=True)
            first_occurrences = self.receptors_expr.columns.duplicated(keep="first")
            self.receptors_expr = self.receptors_expr.loc[:, ~first_occurrences]

        else:
            self.logger.error("Invalid `mod_type` specified. Must be one of 'niche', 'slice', or 'lr'.")

        # Compute initial spatial weights for all samples:
        self.all_spatial_weights = self._compute_all_wi(self.bw)

    # NOTE TO SELF: DURING THE PROCESS OF FINDING THE OPTIMAL BANDWIDTH, RECOMPUTE X AT EACH ITERATION BASED ON THE
    # NEW NEIGHBORHOODS THAT GET RETURNED FROM THE BANDWIDTH SELECTION.

    def _compute_all_wi(self, bw: Union[float, int]) -> np.ndarray:
        """Compute spatial weights for all samples in the dataset given a specified bandwidth.

        Args:
            bw: Bandwidth for the spatial kernel

        Returns:
            wi: Array of weights for all samples in the dataset
        """

        # Parallelized computation of spatial weights for all samples:
        w = np.zeros((self.n_samples, self.n_samples))
        get_wi_partial = partial(
            get_wi, n_samples=self.n_samples, coords=self.coords, fixed_bw=self.bw_fixed, kernel=self.kernel, bw=bw
        )

        with Pool() as pool:
            weights = pool.map(get_wi_partial, range(self.n_samples))
        for i, row in enumerate(weights):
            w[i, :] = row
        return w

    def _adjust_x(self):
        """Adjust the independent variable array based on the defined bandwidth."""
        if self.mod_type:
            "filler"

    def _local_fit(self, i: int):
        """Fit a local regression model for each sample.

        Args:
            i: Index of sample for which local regression model is to be fitted
        """
        wi = self._get_wi(i, self.bw).reshape(-1, 1)

        if self.distr == "gaussian":
            betas, influence_matrix = compute_betas_local(self.y, self.X, wi)
            y_hat = np.dot(self.X[i], betas)[0]
            residual = self.y[i] - y_hat

            # Effect of deleting sample i from the dataset on the estimated coefficients
            influence_i = np.dot(self.X[i], influence_matrix[:, i])
            w = 1

        elif self.distr == "poisson" or self.distr == "nb":
            # init_betas (initial coefficients) to be incorporated at runtime:
            betas, y_hat, n_iter, spatial_weights, linear_predictor, adjusted_predictor, influence_matrix = iwls(
                self.y,
                self.X,
                distr=self.distr,
                init_betas=self.init_params["init_betas"],
            )

    # Main fit function here:

    # Finish putting together appropriate X blocks by combining relevant ligands and receptors if applicable:

    # Selecting optimial bandwidth:


# MGWR:
