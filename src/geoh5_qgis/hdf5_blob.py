"""Read opaque (file-blob) HDF5 datasets that GDAL's multidim API cannot open.

geoh5 stores embedded files (e.g. a GeoImage's JPEG/PNG/TIFF) as HDF5 *opaque*
datasets. GDAL lists them but cannot read them, so this module calls the HDF5 C
library through ``ctypes``. It uses the HDF5 library GDAL has already loaded in
the QGIS process, so nothing extra is installed and the versions always match.

GDAL serialises its own HDF5 calls with an internal lock that ctypes calls
bypass, so a thread-safe HDF5 build is required (QGIS's builds are); calls here
are also serialised with a Python lock.
"""

import ctypes
import ctypes.util
import functools
import sys
import threading
from pathlib import Path

from osgeo import gdal

H5F_ACC_RDONLY = 0
H5P_DEFAULT = 0
H5S_ALL = 0
H5T_OPAQUE = 5

_LOCK = threading.Lock()
_HID = ctypes.c_int64  # hid_t since HDF5 1.10
_PROTOTYPES = {
    "H5open": (ctypes.c_int, []),
    "H5is_library_threadsafe": (ctypes.c_int, [ctypes.POINTER(ctypes.c_bool)]),
    "H5Fopen": (_HID, [ctypes.c_char_p, ctypes.c_uint, _HID]),
    "H5Fclose": (ctypes.c_int, [_HID]),
    "H5Dopen2": (_HID, [_HID, ctypes.c_char_p, _HID]),
    "H5Dclose": (ctypes.c_int, [_HID]),
    "H5Dget_type": (_HID, [_HID]),
    "H5Dget_space": (_HID, [_HID]),
    "H5Dread": (ctypes.c_int, [_HID, _HID, _HID, _HID, _HID, ctypes.c_void_p]),
    "H5Tget_class": (ctypes.c_int, [_HID]),
    "H5Tget_size": (ctypes.c_size_t, [_HID]),
    "H5Tclose": (ctypes.c_int, [_HID]),
    "H5Sget_simple_extent_npoints": (ctypes.c_int64, [_HID]),
    "H5Sclose": (ctypes.c_int, [_HID]),
}


def read_blob(path: str | Path, dataset_path: str) -> bytes:
    """Read the raw bytes of an opaque dataset in an HDF5 (geoh5) file.

    Args:
        path: Path to the .geoh5 file.
        dataset_path: Absolute HDF5 path of the dataset, e.g.
            ``/GEOSCIENCE/Data/{uid}/image.jpg``.

    Returns:
        The dataset's bytes.

    Raises:
        RuntimeError: If the HDF5 library is unavailable or not thread-safe, or
            the dataset cannot be opened or read.
        ValueError: If the dataset is not an opaque (blob) dataset.
    """
    lib = _library()
    with _LOCK:
        file_id = lib.H5Fopen(str(path).encode(), H5F_ACC_RDONLY, H5P_DEFAULT)
        if file_id < 0:
            raise RuntimeError(f"HDF5 could not open {path}")
        try:
            return _read_dataset(lib, file_id, dataset_path)
        finally:
            lib.H5Fclose(file_id)


def _read_dataset(lib: ctypes.CDLL, file_id: int, dataset_path: str) -> bytes:
    dataset = lib.H5Dopen2(file_id, dataset_path.encode(), H5P_DEFAULT)
    if dataset < 0:
        raise RuntimeError(f"HDF5 could not open dataset {dataset_path}")
    dtype = lib.H5Dget_type(dataset)
    space = lib.H5Dget_space(dataset)
    try:
        if lib.H5Tget_class(dtype) != H5T_OPAQUE:
            raise ValueError(f"{dataset_path} is not an opaque (file blob) dataset")
        size = lib.H5Tget_size(dtype) * lib.H5Sget_simple_extent_npoints(space)
        buffer = ctypes.create_string_buffer(size)
        if lib.H5Dread(dataset, dtype, H5S_ALL, H5S_ALL, H5P_DEFAULT, buffer) < 0:
            raise RuntimeError(f"HDF5 could not read {dataset_path}")
        return buffer.raw
    finally:
        lib.H5Tclose(dtype)
        lib.H5Sclose(space)
        lib.H5Dclose(dataset)


@functools.cache
def _library() -> ctypes.CDLL:
    """The HDF5 library GDAL uses, with prototypes declared."""
    gdal.GetDriverByName("HDF5")  # make sure GDAL (and its HDF5) is loaded
    lib = _load_hdf5()
    for name, (restype, argtypes) in _PROTOTYPES.items():
        function = getattr(lib, name)
        function.restype, function.argtypes = restype, argtypes
    lib.H5open()
    threadsafe = ctypes.c_bool()
    lib.H5is_library_threadsafe(ctypes.byref(threadsafe))
    if not threadsafe.value:
        raise RuntimeError("This QGIS build's HDF5 library is not thread-safe")
    return lib


def _load_hdf5() -> ctypes.CDLL:
    names = ["hdf5.dll"] if sys.platform == "win32" else []
    names += [n for n in (ctypes.util.find_library("hdf5"),) if n]
    names += ["libhdf5.so", "libhdf5.dylib"]
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise RuntimeError("Could not find the HDF5 library used by GDAL")
