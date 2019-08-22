import warnings
import numpy as np
import xarray as xr
import geopandas
from rasterio import features
from affine import Affine
from python.aux.utils import calc_area
np.seterr(divide='ignore', invalid='ignore')

"""Contains methods for the flowmodel (transport model & local model)"""


def get_mask_of_basin(da, kw_basins='Danube'):
    """Return a mask where all points outside the selected basin are False.

    Parameters:
    -----------
        da : xr.DataArray
            contains the coordinates
        kw_basins : str
            identifier of the basin in the basins dataset
    """
    def transform_from_latlon(lat, lon):
        lat = np.asarray(lat)
        lon = np.asarray(lon)
        trans = Affine.translation(lon[0], lat[0])
        scale = Affine.scale(lon[1] - lon[0], lat[1] - lat[0])
        return trans * scale

    def rasterize(shapes, coords, fill=np.nan, **kwargs):
        """Rasterize a list of (geometry, fill_value) tuples onto the given
        xray coordinates. This only works for 1d latitude and longitude
        arrays.
        """
        transform = transform_from_latlon(coords['latitude'], coords['longitude'])
        out_shape = (len(coords['latitude']), len(coords['longitude']))
        raster = features.rasterize(shapes, out_shape=out_shape,
                                    fill=fill, transform=transform,
                                    dtype=float, **kwargs)
        return xr.DataArray(raster, coords=coords, dims=('latitude', 'longitude'))

    # this shapefile is from natural earth data
    # http://www.naturalearthdata.com/downloads/10m-cultural-vectors/10m-admin-1-states-provinces/
    shp2 = '/raid/home/srvx7/lehre/users/a1303583/ipython/ml_flood/data/' \
           + 'drainage_basins/Major_Basins_of_the_World.shp'
    basins = geopandas.read_file(shp2)
    single_basin = basins.query("NAME == '"+kw_basins+"'").reset_index(drop=True)
    shapes = [(shape, n) for n, shape in enumerate(single_basin.geometry)]

    da['basins'] = rasterize(shapes, da.coords)
    da = da.basins == 0
    return da.drop('basins')  # the basins coordinate is not used anymore from here on


def select_upstream(mask_river_in_catchment, lat, lon, basin='Danube'):
    """Return a mask containing upstream river gridpoints.

    Parameters
    ----------
        mask_river_in_catchment : xr.DataArray
            array that is True only for river gridpoints within a certain catchment
            coords: only latitude and longitute

        lat, lon : float
            latitude and longitude of the considered point

        basin : str
            identifier of the basin in the basins dataset

    Returns
    -------
    xr.DataArray
        0/1 mask array with (latitude, longitude) as coordinates
    """

    # this condition should be replaced with a terrain dependent mask
    # but generally speaking, there will always be some points returned that
    # do not influence the downstream point;
    # the statistical model should ignore those points as learned from the dataset
    da = mask_river_in_catchment.load()
    is_west = (~np.isnan(da.where(da.longitude <= lon))).astype(bool)

    mask_basin = get_mask_of_basin(da, kw_basins=basin)

    nearby_mask = da*0.
    nearby_mask.loc[dict(latitude=slice(lat+1.5, lat-1.5),
                         longitude=slice(lon-1.5, lon+1.5))] = 1.
    nearby_mask = nearby_mask.astype(bool)

    mask = mask_basin & nearby_mask & is_west & mask_river_in_catchment

    if 'basins' in mask.coords:
        mask = mask.drop('basins')
    if 'time' in mask.coords:
        mask = mask.drop('time')  # time and basins dimension make no sense here
    return mask


def add_shifted_variables(ds, shifts, variables='all'):
    """Adds additional variables to an array which are shifted in time.

    Parameters
    ----------
    ds : xr.Dataset
    shifts : list(int, )
        e.g. range(1,4); shift=1 means having the value x(t=0) at t=1
    variables : str or list
        e.g. ['lsp', 'cp']

    Returns
    -------
    xr.Dataset
        the input Dataset with the shifted timeseries added as additional variable
    """
    if isinstance(ds, xr.DataArray):
        ds = ds.to_dataset()  # enforce input type

    if variables == 'all':
        variables = ds.data_vars

    for var in variables:
        for i in shifts:
            if i == 0:
                continue  # zero-shift is the original timeseries
            if i > 0:
                sign = '-'
            else:
                sign = '+'
            newvar = var+sign+str(i)
            ds[newvar] = ds[var].shift(time=i)
    return ds


def shift_and_aggregate(df, shift, aggregate):
    """
    To get a predictor from [lsp(t-3), ..., lsp(t-6)],
    use shift = 3 and aggregate = 3

    Parameters
    ----------
    shift : int
    aggregate : int
    """
    return df.shift(time=shift).rolling(time=aggregate).sum()/aggregate


def aggregate_clustersum(ds, cluster, clusterdim):
    """Aggregate a 3-dimensional array over certain points (latitude, longitude).

    Parameters
    ----------
    ds : xr.Dataset
        the array to aggregate (collapse) spatially
    cluster : xr.DataArray
        3-dimensional array (clusterdim, latitude, longitude),
        `clusterdim` contains the True/False mask of points to aggregate over
        e.g. len(clusterdim)=4 means you have 4 clusters
    clusterdim : str
        dimension name to access the different True/False masks

    Returns
    -------
    xr.DataArray
        1-dimensional
    """
    out = xr.Dataset()

    # enforce same coordinates
    interp = True
    if (len(ds.latitude.values) == len(cluster.latitude.values) and
            len(ds.longitude.values) == len(cluster.longitude.values)):
        if (np.allclose(ds.latitude.values, cluster.latitude.values) and
                np.allclose(ds.longitude.values, cluster.longitude.values)):
            interp = False
    if interp:
        ds = ds.interp(latitude=cluster.latitude, longitude=cluster.longitude)
    area_per_gridpoint = calc_area(ds.isel(time=0))

    if isinstance(ds, xr.DataArray):
        ds = ds.to_dataset()

    for var in ds:
        for cl in cluster.coords[clusterdim]:
            newname = var+'_cluster'+str(cl.values)
            this_cluster = cluster.sel({clusterdim: cl})

            da = ds[var].where(this_cluster, 0.)  # no contribution from outside cluster
            out[newname] = xr.dot(da, area_per_gridpoint)
    return out.drop(clusterdim)


def cluster_by_discharge(dis_2d, bin_edges):
    """Custom clustering by discharge.
    """
    cluster = dict()
    for i in range(len(bin_edges)-1):
        cluster[str(i)] = (dis_2d >= bin_edges[i]) & (dis_2d < bin_edges[i+1])
        cluster[str(i)].attrs['units'] = None

    return xr.Dataset(cluster,
                      coords=dict(clusterId=('clusterId', range(len(bin_edges))),
                                  latitude=('latitude', dis_2d.latitude),
                                  longitude=('longitude', dis_2d.longitude)))


def reshape_scalar_predictand(X_dis, y):
    """Reshape, merge predictor/predictand in time, drop nans.
    
    Parameters
    ----------
        X_dis : xr.Dataset
            variables: time shifted predictors (name irrelevant)
            coords: time, latitude, longitude
        y : xr.DataArray
            coords: time
    """
    if isinstance(X_dis, xr.Dataset):
        X_dis = X_dis.to_array(dim='var_dimension')

    # stack -> seen as one dimension for the model
    stack_dims = [a for a in X_dis.dims if a != 'time']  # all except time
    X_dis = X_dis.stack(features=stack_dims)
    Xar = X_dis.dropna('features', how='all')  # drop features that only contain NaN

    if isinstance(y, xr.Dataset):
        if len(y.data_vars) > 1:
            warnings.warn('Supplied `y` with more than one variable.'
                          'Which is the predictand? Supply only one!')
        for v in y:
            y = y[v]  # use the first
            break

    yar = y
    if len(yar.dims) > 1:
        raise NotImplementedError('y.dims: '+str(yar.dims) +
                                  ' Supply only one predictand dimension, e.g. `time`!')

    # to be sure that these dims are not in the output
    for coord in ['latitude', 'longitude']:
        if coord in yar.coords:
            yar = yar.drop(coord)

    # merge times
    yar.coords['features'] = 'predictand'
    Xy = xr.concat([Xar, yar], dim='features')  # maybe merge instead concat?
    Xyt = Xy.dropna('time', how='any')  # drop rows with nan values

    Xda = Xyt[:, :-1]  # last column is predictand
    yda = Xyt[:, -1].drop('features')  # features was only needed in merge
    return Xda, yda


def reshape_multiday_predictand(X_dis, y):
    """Reshape, merge predictor/predictand in time, drop nans.
    Parameters
    ----------
        X_dis : xr.Dataset
            variables: time shifted predictors (name irrelevant)
            coords: time, latitude, longitude
        y : xr.DataArray (multiple variables, multiple timesteps)
            coords: time, forecast_day
    """
    if isinstance(X_dis, xr.Dataset):
        X_dis = X_dis.to_array(dim='var_dimension')

    # stack -> seen as one dimension for the model
    stack_dims = [a for a in X_dis.dims if a != 'time']  # all except time
    X_dis = X_dis.stack(features=stack_dims)
    Xar = X_dis.dropna('features', how='all')  # drop features that only contain NaN

    if not isinstance(y, xr.DataArray):
        raise TypeError('Supply `y` as xr.DataArray.'
                        'with coords (time, forecast_day)!')

    # to be sure that these dims are not in the output
    for coord in ['latitude', 'longitude']:
        if coord in y.coords:
            y = y.drop(coord)

    out_dim = len(y.forecast_day)
    y = y.rename(dict(forecast_day='features'))  # rename temporarily
    Xy = xr.concat([Xar, y], dim='features')  # maybe merge instead concat?
    Xyt = Xy.dropna('time', how='any')  # drop rows with nan values

    Xda = Xyt[:, :-out_dim]  # last column is predictand
    yda = Xyt[:, -out_dim:]  # features was only needed in merge
    yda = yda.rename(dict(features='forecast_day'))  # change renaming back to original
    return Xda, yda
