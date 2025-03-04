import os
import pathlib
from collections import OrderedDict
from urllib.request import Request

import numpy as np

import astropy.io.fits
from astropy.utils.decorators import deprecated_renamed_argument
from astropy.wcs import WCS

from sunpy import log
from sunpy.data import cache
from sunpy.io._file_tools import read_file
from sunpy.io.header import FileHeader
from sunpy.map.compositemap import CompositeMap
from sunpy.map.mapbase import GenericMap, MapMetaValidationError
from sunpy.map.mapsequence import MapSequence
from sunpy.util import expand_list
from sunpy.util.datatype_factory_base import (
    BasicRegistrationFactory,
    MultipleMatchError,
    NoMatchError,
    ValidationFunctionError,
)
from sunpy.util.exceptions import NoMapsInFileError, SunpyDeprecationWarning, warn_user
from sunpy.util.functools import seconddispatch
from sunpy.util.io import is_url, parse_path, possibly_a_path
from sunpy.util.metadata import MetaDict

SUPPORTED_ARRAY_TYPES = (np.ndarray,)
try:
    import dask.array
    SUPPORTED_ARRAY_TYPES += (dask.array.Array,)
except ImportError:
    pass

__all__ = ['Map', 'MapFactory']


class MapFactory(BasicRegistrationFactory):
    """
    A factory for generating coordinate aware 2D images.

    This factory takes a variety of inputs, such as file paths, wildcard
    patterns or (data, header) pairs.

    Depending on the input different return types are possible.

    Parameters
    ----------
    \\*inputs
        Inputs to parse for map objects. See the examples section for a
        detailed list of accepted inputs.

    sequence : `bool`, optional
        Return a `sunpy.map.MapSequence` object comprised of all the parsed maps.

    composite : `bool`, optional
        Return a `sunpy.map.CompositeMap` object comprised of all the parsed maps.

    Returns
    -------
    `sunpy.map.GenericMap`
        If the input results in a singular map object, then that is returned.

    `list` of `~sunpy.map.GenericMap`
        If multiple inputs are given and ``sequence=False`` and ``composite=False``
        (the default) then a list of `~sunpy.map.GenericMap` objects will be
        returned.

    `sunpy.map.MapSequence`
        If the input corresponds to multiple maps and ``sequence=True`` is set,
        then a `~sunpy.map.MapSequence` object is returned.

    `sunpy.map.CompositeMap`
        If the input corresponds to multiple maps and ``composite=True`` is set,
        then a `~sunpy.map.CompositeMap` object is returned.

    Examples
    --------
    >>> import sunpy.map
    >>> from astropy.io import fits
    >>> import sunpy.data.sample  # doctest: +REMOTE_DATA
    >>> mymap = sunpy.map.Map(sunpy.data.sample.AIA_171_IMAGE)  # doctest: +REMOTE_DATA
    """

    def _read_file(self, fname, **kwargs):
        """
        Read in a file name and return the list of (data, meta) pairs in that file.
        """
        # File gets read here. This needs to be generic enough to seamlessly
        # call a fits file or a jpeg2k file, etc
        # NOTE: use os.fspath so that fname can be either a str or pathlib.Path
        # This can be removed once read_file supports pathlib.Path
        log.debug(f'Reading {fname}')
        try:
            pairs = read_file(os.fspath(fname), **kwargs)
        except Exception as e:
            msg = f"Failed to read {fname}\n{e}"
            if kwargs.get("silence_errors") or kwargs.get("allow_errors"):
                warn_user(msg)
                return []
            msg += "\n If you want to bypass these errors, pass `allow_errors=True`."
            raise OSError(msg) from e

        new_pairs = []
        for pair in pairs:
            filedata, filemeta = pair
            assert isinstance(filemeta, FileHeader)
            # This tests that the data is more than 1D
            if len(np.shape(filedata)) > 1:
                data = filedata
                meta = MetaDict(filemeta)
                new_pairs.append((data, meta))

        if not new_pairs:
            raise NoMapsInFileError(f"Found no HDUs with >= 2D data in '{fname}'.")

        return new_pairs

    def _validate_meta(self, meta):
        """
        Validate a meta argument.
        """
        if isinstance(meta, astropy.io.fits.header.Header):
            return True
        elif isinstance(meta, dict):
            return True
        else:
            return False

    def _parse_args(self, *args, silence_errors=False, allow_errors=False, **kwargs):
        """
        Parses an args list into data-header pairs.

        args can contain any mixture of the following entries:
        * tuples of data,header
        * data, header not in a tuple
        * data, wcs object in a tuple
        * data, wcs object not in a tuple
        * filename, as a str or pathlib.Path, which will be read
        * directory, as a str or pathlib.Path, from which all files will be read
        * glob, from which all files will be read
        * url, which will be downloaded and read
        * lists containing any of the above.

        Examples
        --------
        self._parse_args(data, header,
                         (data, header),
                         ['file1', 'file2', 'file3'],
                         'file4',
                         'directory1',
                         '*.fits')
        """
        # Account for nested lists of items
        args = expand_list(args)

        # Sanitise the input so that each 'type' of input corresponds to a different
        # class, so single dispatch can be used later
        nargs = len(args)
        i = 0
        while i < nargs:
            arg = args[i]
            if isinstance(arg, SUPPORTED_ARRAY_TYPES):
                # The next two items are data and a header
                data = args.pop(i)
                header = args.pop(i)
                args.insert(i, (data, header))
                nargs -= 1
            elif isinstance(arg, str) and is_url(arg):
                # Repalce URL string with a Request object to dispatch on later
                args[i] = Request(arg)
            elif possibly_a_path(arg):
                # Repalce path strings with Path objects
                args[i] = pathlib.Path(arg)
            i += 1

        # Parse the arguments
        # Note that this list can also contain GenericMaps if they are directly given to the factory
        data_header_pairs = []
        for arg in args:
            try:
                data_header_pairs += self._parse_arg(arg, silence_errors=silence_errors, allow_errors=allow_errors,**kwargs)
            except NoMapsInFileError as e:
                if not (silence_errors or allow_errors):
                    raise
                warn_user(f"One of the arguments failed to parse with error: {e}")

        return data_header_pairs

    # Note that post python 3.8 this can be @functools.singledispatchmethod
    @seconddispatch
    def _parse_arg(self, arg, **kwargs):
        """
        Take a factory input and parse into (data, header) pairs.
        Must return a list, even if only one pair is returned.
        """
        raise ValueError(f"Invalid input: {arg}")

    @_parse_arg.register(tuple)
    def _parse_tuple(self, arg, **kwargs):
        # Data-header or data-WCS pair
        data, header = arg
        if isinstance(header, WCS):
            header = header.to_header()

        pair = data, header
        if self._validate_meta(header):
            pair = (data, OrderedDict(header))
        return [pair]

    @_parse_arg.register(GenericMap)
    def _parse_map(self, arg, **kwargs):
        return [arg]

    @_parse_arg.register(Request)
    def _parse_url(self, arg, **kwargs):
        url = arg.full_url
        path = str(cache.download(url).absolute())
        pairs = self._read_file(path, **kwargs)
        return pairs

    @_parse_arg.register(pathlib.Path)
    def _parse_path(self, arg, **kwargs):
        return parse_path(arg, self._read_file, **kwargs)

    @deprecated_renamed_argument("silence_errors","allow_errors","5.1", warning_type=SunpyDeprecationWarning)
    def __call__(self, *args, composite=False, sequence=False, silence_errors=False, allow_errors=False, **kwargs):
        """ Method for running the factory. Takes arbitrary arguments and
        keyword arguments and passes them to a sequence of pre-registered types
        to determine which is the correct Map-type to build.

        Arguments args and kwargs are passed through to the validation
        function and to the constructor for the final type. For Map types,
        validation function must take a data-header pair as an argument.

        Parameters
        ----------
        composite : `bool`, optional
            Indicates if collection of maps should be returned as a `~sunpy.map.CompositeMap`.
            Default is `False`.
        sequence : `bool`, optional
            Indicates if collection of maps should be returned as a `sunpy.map.MapSequence`.
            Default is `False`.
        silence_errors : `bool`, optional
            Deprecated, renamed to `allow_errors`.

            If set, ignore data-header pairs which cause an exception.
            Default is ``False``.
        allow_errors : `bool`, optional
            If set, bypass data-header pairs or files which cause an exception and warn instead.
            Defaults to `False`.

        Notes
        -----
        Extra keyword arguments are passed through to `sunpy.io.read_file` such
        as `memmap` for FITS files.
        """
        data_header_pairs = self._parse_args(*args, silence_errors=silence_errors, allow_errors=allow_errors, **kwargs)
        new_maps = list()

        # Loop over each registered type and check to see if WidgetType
        # matches the arguments.  If it does, use that type.
        for pair in data_header_pairs:
            if isinstance(pair, GenericMap):
                new_maps.append(pair)
                continue
            data, header = pair
            meta = MetaDict(header)

            try:
                new_map = self._check_registered_widgets(data, meta, **kwargs)
                new_maps.append(new_map)
            except (NoMatchError, MultipleMatchError,
                    ValidationFunctionError, MapMetaValidationError) as e:
                if not (silence_errors or allow_errors):
                    raise
                warn_user(f"One of the data, header pairs failed to validate with: {e}")

        if not len(new_maps):
            raise RuntimeError('No maps loaded')

        # If the list is meant to be a sequence, instantiate a map sequence
        if sequence:
            return MapSequence(new_maps, **kwargs)

        # If the list is meant to be a composite map, instantiate one
        if composite:
            return CompositeMap(new_maps, **kwargs)

        if len(new_maps) == 1:
            return new_maps[0]

        return new_maps

    def _check_registered_widgets(self, data, meta, **kwargs):

        candidate_widget_types = list()

        for key in self.registry:

            # Call the registered validation function for each registered class
            if self.registry[key](data, meta, **kwargs):
                candidate_widget_types.append(key)

        n_matches = len(candidate_widget_types)

        if n_matches == 0:
            if self.default_widget_type is None:
                raise NoMatchError("No types match specified arguments and no default is set.")
            else:
                candidate_widget_types = [self.default_widget_type]
        elif n_matches > 1:
            raise MultipleMatchError("Too many candidate types identified "
                                     f"({candidate_widget_types}). "
                                     "Specify enough keywords to guarantee unique type "
                                     "identification.")

        # Only one is found
        WidgetType = candidate_widget_types[0]

        return WidgetType(data, meta, **kwargs)


class InvalidMapInput(ValueError):
    """Exception to raise when input variable is not a Map instance and does
    not point to a valid Map input file."""


class InvalidMapType(ValueError):
    """Exception to raise when an invalid type of map is requested with Map
    """


class NoMapsFound(ValueError):
    """Exception to raise when input does not point to any valid maps or files
    """


Map = MapFactory(registry=GenericMap._registry, default_widget_type=GenericMap,
                 additional_validation_functions=['is_datasource_for'])
