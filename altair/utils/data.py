import json
import os
import random
import hashlib
import warnings
from importlib.metadata import version as importlib_version, PackageNotFoundError
from typing import Union, MutableMapping, Optional, Dict, Sequence, TYPE_CHECKING, List
from types import ModuleType

import pandas as pd
from toolz import curried
from typing import TypeVar
from packaging.version import Version

from .core import sanitize_dataframe, sanitize_arrow_table, _DataFrameLike
from .core import sanitize_geo_interface
from .deprecation import AltairDeprecationWarning
from .plugin_registry import PluginRegistry


from typing import Protocol, TypedDict, Literal


if TYPE_CHECKING:
    import pyarrow.lib


class _SupportsGeoInterface(Protocol):
    __geo_interface__: MutableMapping


_DataType = Union[dict, pd.DataFrame, _SupportsGeoInterface, _DataFrameLike]
_TDataType = TypeVar("_TDataType", bound=_DataType)

_VegaLiteDataDict = Dict[str, Union[str, dict, List[dict]]]
_ToValuesReturnType = Dict[str, Union[dict, List[dict]]]


# ==============================================================================
# Data transformer registry
#
# A data transformer is a callable that takes a supported data type and returns
# a transformed dictionary version of it which is compatible with the VegaLite schema.
# The dict objects will be the Data portion of the VegaLite schema.
#
# Renderers only deal with the dict form of a
# VegaLite spec, after the Data model has been put into a schema compliant
# form.
# ==============================================================================
class DataTransformerType(Protocol):
    def __call__(self, data: _DataType, **kwargs) -> _VegaLiteDataDict:
        pass


class DataTransformerRegistry(PluginRegistry[DataTransformerType]):
    _global_settings = {"consolidate_datasets": True}

    @property
    def consolidate_datasets(self) -> bool:
        return self._global_settings["consolidate_datasets"]

    @consolidate_datasets.setter
    def consolidate_datasets(self, value: bool) -> None:
        self._global_settings["consolidate_datasets"] = value


# ==============================================================================
class MaxRowsError(Exception):
    """Raised when a data model has too many rows."""

    pass


@curried.curry
def limit_rows(data: _TDataType, max_rows: Optional[int] = 5000) -> _TDataType:
    """Raise MaxRowsError if the data model has more than max_rows.

    If max_rows is None, then do not perform any check.
    """
    check_data_type(data)

    def raise_max_rows_error():
        raise MaxRowsError(
            "The number of rows in your dataset is greater "
            f"than the maximum allowed ({max_rows}).\n\n"
            "Try enabling the VegaFusion data transformer which "
            "raises this limit by pre-evaluating data\n"
            "transformations in Python.\n"
            "    >> import altair as alt\n"
            '    >> alt.data_transformers.enable("vegafusion")\n\n'
            "Or, see https://altair-viz.github.io/user_guide/large_datasets.html "
            "for additional information\n"
            "on how to plot large datasets."
        )

    if hasattr(data, "__geo_interface__"):
        if data.__geo_interface__["type"] == "FeatureCollection":
            values = data.__geo_interface__["features"]
        else:
            values = data.__geo_interface__
    elif isinstance(data, pd.DataFrame):
        values = data
    elif isinstance(data, dict):
        if "values" in data:
            values = data["values"]
        else:
            # mypy gets confused as it doesn't see Dict[Any, Any]
            # as equivalent to TDataType
            return data  # type: ignore[return-value]
    elif hasattr(data, "__dataframe__"):
        pi = import_pyarrow_interchange()
        pa_table = pi.from_dataframe(data)
        if max_rows is not None and pa_table.num_rows > max_rows:
            raise_max_rows_error()
        # Return pyarrow Table instead of input since the
        # `from_dataframe` call may be expensive
        return pa_table

    if max_rows is not None and len(values) > max_rows:
        raise_max_rows_error()

    return data


@curried.curry
def sample(
    data: _DataType, n: Optional[int] = None, frac: Optional[float] = None
) -> Optional[Union[pd.DataFrame, Dict[str, Sequence], "pyarrow.lib.Table"]]:
    """Reduce the size of the data model by sampling without replacement."""
    check_data_type(data)
    if isinstance(data, pd.DataFrame):
        return data.sample(n=n, frac=frac)
    elif isinstance(data, dict):
        if "values" in data:
            values = data["values"]
            if not n:
                if frac is None:
                    raise ValueError(
                        "frac cannot be None if n is None and data is a dictionary"
                    )
                n = int(frac * len(values))
            values = random.sample(values, n)
            return {"values": values}
        else:
            # Maybe this should raise an error or return something useful?
            return None
    elif hasattr(data, "__dataframe__"):
        # experimental interchange dataframe support
        pi = import_pyarrow_interchange()
        pa_table = pi.from_dataframe(data)
        if not n:
            if frac is None:
                raise ValueError(
                    "frac cannot be None if n is None with this data input type"
                )
            n = int(frac * len(pa_table))
        indices = random.sample(range(len(pa_table)), n)
        return pa_table.take(indices)
    else:
        # Maybe this should raise an error or return something useful? Currently,
        # if data is of type SupportsGeoInterface it lands here
        return None


class _JsonFormatDict(TypedDict):
    type: Literal["json"]


class _CsvFormatDict(TypedDict):
    type: Literal["csv"]


class _ToJsonReturnUrlDict(TypedDict):
    url: str
    format: _JsonFormatDict


class _ToCsvReturnUrlDict(TypedDict):
    url: str
    format: _CsvFormatDict


@curried.curry
def to_json(
    data: _DataType,
    prefix: str = "altair-data",
    extension: str = "json",
    filename: str = "{prefix}-{hash}.{extension}",
    urlpath: str = "",
) -> _ToJsonReturnUrlDict:
    """
    Write the data model to a .json file and return a url based data model.
    """
    data_json = _data_to_json_string(data)
    data_hash = _compute_data_hash(data_json)
    filename = filename.format(prefix=prefix, hash=data_hash, extension=extension)
    with open(filename, "w") as f:
        f.write(data_json)
    return {"url": os.path.join(urlpath, filename), "format": {"type": "json"}}


@curried.curry
def to_csv(
    data: Union[dict, pd.DataFrame, _DataFrameLike],
    prefix: str = "altair-data",
    extension: str = "csv",
    filename: str = "{prefix}-{hash}.{extension}",
    urlpath: str = "",
) -> _ToCsvReturnUrlDict:
    """Write the data model to a .csv file and return a url based data model."""
    data_csv = _data_to_csv_string(data)
    data_hash = _compute_data_hash(data_csv)
    filename = filename.format(prefix=prefix, hash=data_hash, extension=extension)
    with open(filename, "w") as f:
        f.write(data_csv)
    return {"url": os.path.join(urlpath, filename), "format": {"type": "csv"}}


@curried.curry
def to_values(data: _DataType) -> _ToValuesReturnType:
    """Replace a DataFrame by a data model with values."""
    check_data_type(data)
    if hasattr(data, "__geo_interface__"):
        if isinstance(data, pd.DataFrame):
            data = sanitize_dataframe(data)
        # Maybe the type could be further clarified here that it is
        # SupportGeoInterface and then the ignore statement is not needed?
        data_sanitized = sanitize_geo_interface(data.__geo_interface__)  # type: ignore[arg-type]
        return {"values": data_sanitized}
    elif isinstance(data, pd.DataFrame):
        data = sanitize_dataframe(data)
        return {"values": data.to_dict(orient="records")}
    elif isinstance(data, dict):
        if "values" not in data:
            raise KeyError("values expected in data dict, but not present.")
        return data
    elif hasattr(data, "__dataframe__"):
        # experimental interchange dataframe support
        pi = import_pyarrow_interchange()
        pa_table = sanitize_arrow_table(pi.from_dataframe(data))
        return {"values": pa_table.to_pylist()}
    else:
        # Should never reach this state as tested by check_data_type
        raise ValueError("Unrecognized data type: {}".format(type(data)))


def check_data_type(data: _DataType) -> None:
    if not isinstance(data, (dict, pd.DataFrame)) and not any(
        hasattr(data, attr) for attr in ["__geo_interface__", "__dataframe__"]
    ):
        raise TypeError(
            "Expected dict, DataFrame or a __geo_interface__ attribute, got: {}".format(
                type(data)
            )
        )


# ==============================================================================
# Private utilities
# ==============================================================================
def _compute_data_hash(data_str: str) -> str:
    return hashlib.md5(data_str.encode()).hexdigest()


def _data_to_json_string(data: _DataType) -> str:
    """Return a JSON string representation of the input data"""
    check_data_type(data)
    if hasattr(data, "__geo_interface__"):
        if isinstance(data, pd.DataFrame):
            data = sanitize_dataframe(data)
        # Maybe the type could be further clarified here that it is
        # SupportGeoInterface and then the ignore statement is not needed?
        data = sanitize_geo_interface(data.__geo_interface__)  # type: ignore[arg-type]
        return json.dumps(data)
    elif isinstance(data, pd.DataFrame):
        data = sanitize_dataframe(data)
        return data.to_json(orient="records", double_precision=15)
    elif isinstance(data, dict):
        if "values" not in data:
            raise KeyError("values expected in data dict, but not present.")
        return json.dumps(data["values"], sort_keys=True)
    elif hasattr(data, "__dataframe__"):
        # experimental interchange dataframe support
        pi = import_pyarrow_interchange()
        pa_table = pi.from_dataframe(data)
        return json.dumps(pa_table.to_pylist())
    else:
        raise NotImplementedError(
            "to_json only works with data expressed as " "a DataFrame or as a dict"
        )


def _data_to_csv_string(data: Union[dict, pd.DataFrame, _DataFrameLike]) -> str:
    """return a CSV string representation of the input data"""
    check_data_type(data)
    if hasattr(data, "__geo_interface__"):
        raise NotImplementedError(
            "to_csv does not work with data that "
            "contains the __geo_interface__ attribute"
        )
    elif isinstance(data, pd.DataFrame):
        data = sanitize_dataframe(data)
        return data.to_csv(index=False)
    elif isinstance(data, dict):
        if "values" not in data:
            raise KeyError("values expected in data dict, but not present")
        return pd.DataFrame.from_dict(data["values"]).to_csv(index=False)
    elif hasattr(data, "__dataframe__"):
        # experimental interchange dataframe support
        pi = import_pyarrow_interchange()
        import pyarrow as pa
        import pyarrow.csv as pa_csv

        pa_table = pi.from_dataframe(data)
        csv_buffer = pa.BufferOutputStream()
        pa_csv.write_csv(pa_table, csv_buffer)
        return csv_buffer.getvalue().to_pybytes().decode()
    else:
        raise NotImplementedError(
            "to_csv only works with data expressed as " "a DataFrame or as a dict"
        )


def pipe(data, *funcs):
    """
    Pipe a value through a sequence of functions

    Deprecated: use toolz.curried.pipe() instead.
    """
    warnings.warn(
        "alt.pipe() is deprecated, and will be removed in a future release. "
        "Use toolz.curried.pipe() instead.",
        AltairDeprecationWarning,
        stacklevel=1,
    )
    return curried.pipe(data, *funcs)


def curry(*args, **kwargs):
    """Curry a callable function

    Deprecated: use toolz.curried.curry() instead.
    """
    warnings.warn(
        "alt.curry() is deprecated, and will be removed in a future release. "
        "Use toolz.curried.curry() instead.",
        AltairDeprecationWarning,
        stacklevel=1,
    )
    return curried.curry(*args, **kwargs)


def import_pyarrow_interchange() -> ModuleType:
    try:
        pyarrow_version_str = importlib_version("pyarrow")
    except PackageNotFoundError as err:
        raise ImportError(
            "Usage of the DataFrame Interchange Protocol requires the package"
            + " 'pyarrow', but it is not installed."
        ) from err
    else:
        if Version(pyarrow_version_str) < Version("11.0.0"):
            raise ImportError(
                "The installed version of 'pyarrow' does not meet the minimum requirement of version 11.0.0. "
                "Please update 'pyarrow' to use the DataFrame Interchange Protocol."
            )
        else:
            import pyarrow.interchange as pi

            return pi


def pyarrow_available() -> bool:
    try:
        import_pyarrow_interchange()
        return True
    except ImportError:
        return False
