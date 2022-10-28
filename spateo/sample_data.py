import ntpath
import os
from pathlib import Path
from typing import Optional, Tuple, Union
from urllib.request import urlretrieve

from anndata import AnnData, read_h5ad, read_loom

from .logging import logger_manager as lm


def download_data(url: str, file_path: Optional[str] = None, dir_name: str = "./data") -> str:
    """Parse url to get the file name and then download the data to designated folders.

    Args:
        url: url that deposits the data.
        file_path: file path that will store the data locally.
        dir_name: name of the directory.

    Returns:
        the file path that points to the downloaded data.
    """

    file_path = ntpath.basename(url) if file_path is None else file_path
    file_path = os.path.join(dir_name, file_path)
    lm.main_info("Downloading data to " + file_path)

    if not os.path.exists(file_path):
        if not os.path.exists("./data/"):
            os.mkdir("data")

        # download the data
        urlretrieve(url, file_path, reporthook=lm.get_main_logger().request_report_hook)

    return file_path


def get_adata(url: str, filename: Optional[str] = None) -> AnnData:
    """Download example data to local folder.

    Args:
        url: url that deposits the data.
        filename: file name that will store the data locally.

    Returns:
        adata: :class:`~anndata.AnnData`
            an Annodata object.
    """

    file_path = download_data(url, filename)
    if Path(file_path).suffixes[-1][1:] == "loom":
        adata = read_loom(filename=file_path)
    elif Path(file_path).suffixes[-1][1:] == "h5ad":
        adata = read_h5ad(filename=file_path)

    adata.var_names_make_unique()

    return adata


def drosophila_E8_10(
    url="https://www.dropbox.com/s/z330s7a4p2w15oe/E8-10_b_all_anno_scsq.h5ad?dl=1",
    filename="drosophila_E8_10.h5ad",
):
    """The E8-10 whole-body data of drosophila of aligned serial slices with tissue type annotations.

    Returns:
        Returns `adata` object
    """
    adata = get_adata(url, filename)

    return adata


def slideseq(
    url="https://www.dropbox.com/s/d3tpusisbyzn6jk/slideseq.h5ad?dl=1",
    filename="slideseq_mouse_hippocampus.h5ad",
):
    """Saptial transcriptomic sample from the mouse hippocampus; data generated using Slide-seqV2. See:
    Stickels, R. R., Murray, E., Kumar, P., Li, J., Marshall, J. L., Di Bella, D. J., ... & Chen, F. (2021).
    Highly sensitive spatial transcriptomics at near-cellular resolution with Slide-seqV2. Nature biotechnology, 39(3),
    313-319.

    Returns:
        adata: AnnData object containing Slide-seq data
    """
    adata = get_adata(url, filename)

    return adata


def seqfish(
    url="https://www.dropbox.com/s/cm3uw8czhz5hu30/seqFISH.h5ad?dl=1",
    filename="seqfish_mouse_embryo.h5ad",
):
    """Spatial transcriptomic sample taken at one timepoint in the process of mouse organogenesis; data generated using
    seqFISH. See:
    Lohoff, T., Ghazanfar, S., Missarova, A., Koulena, N., Pierson, N., Griffiths, J. A., ... & Marioni, J. C. (2022).
    Integration of spatial and single-cell transcriptomic data elucidates mouse organogenesis.
    Nature biotechnology, 40(1), 74-85.

    Returns:
        adata: AnnData object containing Slide-seq data
    """
    adata = get_adata(url, filename)

    return adata


def merfish(
    url="https://www.dropbox.com/s/e8hwgqnrx2ob9h4/MERFISH.h5ad?dl=1",
    filename="merfish_mouse_hypothalamus.h5ad",
):
    """Spatial transcriptomic sample taken from the mouse hypothalamus; data generated using MERFISH. See:
    Moffitt, J. R., Bambah-Mukku, D., Eichhorn, S. W., Vaughn, E., Shekhar, K., Perez, J. D., ... & Zhuang, X. (2018).
    Molecular, spatial, and functional single-cell profiling of the hypothalamic preoptic region.
    Science, 362(6416), eaau5324.

    Returns:
        adata: AnnData object containing Slide-seq data
    """
    adata = get_adata(url, filename)

    return adata


def seqscope(
    url="https://www.dropbox.com/s/hci9up23dkuyexb/SeqScope.h5ad?dl=1",
    filename="seqscope_mouse_liver.h5ad",
):
    """Spatial transcriptomic sample taken from the mouse liver; data generated using Seq-Scope. See:
    Cho, C. S., Xi, J., Si, Y., Park, S. R., Hsu, J. E., Kim, M., ... & Lee, J. H. (2021). Microscopic examination of
    spatial transcriptome using Seq-Scope. Cell, 184(13), 3559-3572, and:
    Xi, J., Lee, J. H., Kang, H. M., & Jun, G. (2022). STtools: a comprehensive software pipeline for
    ultra-high-resolution spatial transcriptomics data. Bioinformatics Advances, 2(1), vbac061.

    Returns:
        adata: AnnData object containing Slide-seq data
    """
    adata = get_adata(url, filename)

    return adata


def starmap(
    url="https://www.dropbox.com/s/zpvu387tajrwth7/STARmap.h5ad?dl=1",
    filename="starmap_mouse_brain.h5ad",
):
    """Spatial transcriptomic sample taken from the mouse brain; data generated using STARmap. See:
    Wang, X., Allen, W. E., Wright, M. A., Sylwestrak, E. L., Samusik, N., Vesuna, S., ... & Deisseroth, K. (2018).
    Three-dimensional intact-tissue sequencing of single-cell transcriptional states. Science, 361(6400), eaat5691.

    Returns:
        adata: AnnData object containing Slide-seq data
    """
    adata = get_adata(url, filename)

    return adata
