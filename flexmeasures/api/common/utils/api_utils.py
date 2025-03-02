from timely_beliefs.beliefs.classes import BeliefsDataFrame
from typing import List, Sequence, Tuple, Union
import copy
from datetime import datetime, timedelta
from json import loads as parse_json, JSONDecodeError

from flask import current_app
from inflection import pluralize
from numpy import array
from psycopg2.errors import UniqueViolation
from rq.job import Job
from sqlalchemy.exc import IntegrityError
import timely_beliefs as tb

from flexmeasures.data import db
from flexmeasures.data.models.assets import Asset, Power
from flexmeasures.data.models.generic_assets import GenericAsset, GenericAssetType
from flexmeasures.data.models.markets import Price
from flexmeasures.data.models.time_series import Sensor, TimedBelief
from flexmeasures.data.models.weather import WeatherSensor, Weather
from flexmeasures.data.services.time_series import drop_unchanged_beliefs
from flexmeasures.data.utils import save_to_session, save_to_db as modern_save_to_db
from flexmeasures.api.common.responses import (
    invalid_replacement,
    unrecognized_sensor,
    ResponseTuple,
    request_processed,
    already_received_and_successfully_processed,
)
from flexmeasures.utils.error_utils import error_handling_router


def list_access(service_listing, service_name):
    """
    For a given USEF service name (API endpoint) in a service listing,
    return the list of USEF roles that are allowed to access the service.
    """
    return next(
        service["access"]
        for service in service_listing["services"]
        if service["name"] == service_name
    )


def contains_empty_items(groups: List[List[str]]):
    """
    Return True if any of the items in the groups is empty.
    """
    for group in groups:
        for item in group:
            if item == "" or item is None:
                return True
    return False


def parse_as_list(
    connection: Union[Sequence[Union[str, float]], str, float], of_type: type = None
) -> Sequence[Union[str, float, None]]:
    """
    Return a list of connections (or values), even if it's just one connection (or value)
    """
    connections: Sequence[Union[str, float, None]] = []
    if not isinstance(connection, list):
        if of_type is None:
            connections = [connection]  # type: ignore
        else:
            try:
                connections = [of_type(connection)]
            except TypeError:
                connections = [None]
    else:  # key should have been plural
        if of_type is None:
            connections = connection
        else:
            try:
                connections = [of_type(c) for c in connection]
            except TypeError:
                connections = [None]
    return connections


# TODO: we should be using webargs to get data from a request, it's more descriptive and has error handling
def get_form_from_request(_request) -> Union[dict, None]:
    if _request.method == "GET":
        d = _request.args.to_dict(
            flat=False
        )  # From MultiDict, obtain all values with the same key as a list
        parsed_d = {}
        for k, v_list in d.items():
            parsed_v_list = []
            for v in v_list:
                try:
                    parsed_v = parse_json(v)
                except JSONDecodeError:
                    parsed_v = v
                if isinstance(parsed_v, list):
                    parsed_v_list.extend(parsed_v)
                else:
                    parsed_v_list.append(v)
            if len(parsed_v_list) == 1:  # Flatten single-value lists
                parsed_d[k] = parsed_v_list[0]
            else:
                parsed_d[k] = parsed_v_list
        return parsed_d
    elif _request.method == "POST":
        return _request.get_json(force=True)
    else:
        return None


def append_doc_of(fun):
    def decorator(f):
        if f.__doc__:
            f.__doc__ += fun.__doc__
        else:
            f.__doc__ = fun.__doc__
        return f

    return decorator


def upsample_values(
    value_groups: Union[List[List[float]], List[float]],
    from_resolution: timedelta,
    to_resolution: timedelta,
) -> Union[List[List[float]], List[float]]:
    """Upsample the values (in value groups) to a smaller resolution.
    from_resolution has to be a multiple of to_resolution"""
    if from_resolution % to_resolution == timedelta(hours=0):
        n = from_resolution // to_resolution
        if isinstance(value_groups[0], list):
            value_groups = [
                list(array(value_group).repeat(n)) for value_group in value_groups
            ]
        else:
            value_groups = list(array(value_groups).repeat(n))
    return value_groups


def groups_to_dict(
    connection_groups: List[str],
    value_groups: List[List[str]],
    generic_asset_type_name: str,
    plural_name: str = None,
    groups_name="groups",
) -> dict:
    """Put the connections and values in a dictionary and simplify if groups have identical values and/or if there is
    only one group.

    Examples:

        >> connection_groups = [[1]]
        >> value_groups = [[300, 300, 300]]
        >> response_dict = groups_to_dict(connection_groups, value_groups, "connection")
        >> print(response_dict)
        <<  {
                "connection": 1,
                "values": [300, 300, 300]
            }

        >> connection_groups = [[1], [2]]
        >> value_groups = [[300, 300, 300], [300, 300, 300]]
        >> response_dict = groups_to_dict(connection_groups, value_groups, "connection")
        >> print(response_dict)
        <<  {
                "connections": [1, 2],
                "values": [300, 300, 300]
            }

        >> connection_groups = [[1], [2]]
        >> value_groups = [[300, 300, 300], [400, 400, 400]]
        >> response_dict = groups_to_dict(connection_groups, value_groups, "connection")
        >> print(response_dict)
        <<  {
                "groups": [
                    {
                        "connection": 1,
                        "values": [300, 300, 300]
                    },
                    {
                        "connection": 2,
                        "values": [400, 400, 400]
                    }
                ]
            }
    """

    if plural_name is None:
        plural_name = pluralize(generic_asset_type_name)

    # Simplify groups that have identical values
    value_groups, connection_groups = unique_ever_seen(value_groups, connection_groups)

    # Simplify if there is only one group
    if len(value_groups) == len(connection_groups) == 1:
        if len(connection_groups[0]) == 1:
            return {
                generic_asset_type_name: connection_groups[0][0],
                "values": value_groups[0],
            }
        else:
            return {plural_name: connection_groups[0], "values": value_groups[0]}
    else:
        d: dict = {groups_name: []}
        for connection_group, value_group in zip(connection_groups, value_groups):
            if len(connection_group) == 1:
                d[groups_name].append(
                    {
                        generic_asset_type_name: connection_group[0],
                        "values": value_group,
                    }
                )
            else:
                d[groups_name].append(
                    {plural_name: connection_group, "values": value_group}
                )
        return d


def unique_ever_seen(iterable: Sequence, selector: Sequence):
    """
    Return unique iterable elements with corresponding lists of selector elements, preserving order.

    >>> a, b = unique_ever_seen([[10, 20], [10, 20], [20, 40]], [1, 2, 3])
    >>> print(a)
    [[10, 20], [20, 40]]
    >>> print(b)
    [[1, 2], 3]
    """
    u = []
    s = []
    for iterable_element, selector_element in zip(iterable, selector):
        if iterable_element not in u:
            u.append(iterable_element)
            s.append(selector_element)
        else:
            us = s[u.index(iterable_element)]
            if not isinstance(us, list):
                us = [us]
            us.append(selector_element)
            s[u.index(iterable_element)] = us
    return u, s


def message_replace_name_with_ea(message_with_connections_as_asset_names: dict) -> dict:
    """
    For each connection in the message specified by a name, replace that name with the correct entity address.
    TODO: This function is now only used in tests and should go (also asset_replace_name_with_id)
    """
    message_with_connections_as_eas = copy.deepcopy(
        message_with_connections_as_asset_names
    )
    if "connection" in message_with_connections_as_asset_names:
        message_with_connections_as_eas["connection"] = asset_replace_name_with_id(
            parse_as_list(  # type:ignore
                message_with_connections_as_eas["connection"], of_type=str
            )
        )
    elif "connections" in message_with_connections_as_asset_names:
        message_with_connections_as_eas["connections"] = asset_replace_name_with_id(
            parse_as_list(  # type:ignore
                message_with_connections_as_eas["connections"], of_type=str
            )
        )
    elif "groups" in message_with_connections_as_asset_names:
        for i, group in enumerate(message_with_connections_as_asset_names["groups"]):
            if "connection" in group:
                message_with_connections_as_eas["groups"][i][
                    "connection"
                ] = asset_replace_name_with_id(
                    parse_as_list(group["connection"], of_type=str)  # type:ignore
                )
            elif "connections" in group:
                message_with_connections_as_eas["groups"][i][
                    "connections"
                ] = asset_replace_name_with_id(
                    parse_as_list(group["connections"], of_type=str)  # type:ignore
                )
    return message_with_connections_as_eas


def asset_replace_name_with_id(connections_as_name: List[str]) -> List[str]:
    """Look up the owner and id given the asset name and construct a type 1 USEF entity address."""
    connections_as_ea = []
    for asset_name in connections_as_name:
        asset = Asset.query.filter(Asset.name == asset_name).one_or_none()
        connections_as_ea.append(asset.entity_address)
    return connections_as_ea


def get_sensor_by_generic_asset_type_and_location(
    generic_asset_type_name: str, latitude: float = 0, longitude: float = 0
) -> Union[Sensor, ResponseTuple]:
    """
    Search a sensor by generic asset type and location.
    Can create a sensor if needed (depends on API mode)
    and then inform the requesting user which one to use.
    """
    # Look for the Sensor object
    sensor = (
        Sensor.query.join(GenericAsset)
        .join(GenericAssetType)
        .filter(GenericAssetType.name == generic_asset_type_name)
        .filter(GenericAsset.generic_asset_type_id == GenericAssetType.id)
        .filter(GenericAsset.latitude == latitude)
        .filter(GenericAsset.longitude == longitude)
        .filter(Sensor.generic_asset_id == GenericAsset.id)
        .one_or_none()
    )
    if sensor is None:
        create_sensor_if_unknown = False
        if current_app.config.get("FLEXMEASURES_MODE", "") == "play":
            create_sensor_if_unknown = True

        # either create a new weather sensor and post to that
        if create_sensor_if_unknown:
            current_app.logger.info("CREATING NEW WEATHER SENSOR...")
            weather_sensor = WeatherSensor(
                name="Weather sensor for %s at latitude %s and longitude %s"
                % (generic_asset_type_name, latitude, longitude),
                weather_sensor_type_name=generic_asset_type_name,
                latitude=latitude,
                longitude=longitude,
            )
            db.session.add(weather_sensor)
            db.session.flush()  # flush so that we can reference the new object in the current db session
            sensor = weather_sensor.corresponding_sensor

        # or query and return the nearest sensor and let the requesting user post to that one
        else:
            nearest_weather_sensor = WeatherSensor.query.order_by(
                WeatherSensor.great_circle_distance(
                    latitude=latitude, longitude=longitude
                ).asc()
            ).first()
            if nearest_weather_sensor is not None:
                return unrecognized_sensor(
                    *nearest_weather_sensor.location,
                )
            else:
                return unrecognized_sensor()
    return sensor


def enqueue_forecasting_jobs(
    forecasting_jobs: List[Job] = None,
):
    """Enqueue forecasting jobs.

    :param forecasting_jobs: list of forecasting Jobs for redis queues.
    """
    if forecasting_jobs is not None:
        [current_app.queues["forecasting"].enqueue_job(job) for job in forecasting_jobs]


def save_and_enqueue(
    data: Union[BeliefsDataFrame, List[BeliefsDataFrame]],
    forecasting_jobs: List[Job] = None,
    save_changed_beliefs_only: bool = True,
) -> ResponseTuple:

    # Attempt to save
    status = modern_save_to_db(
        data, save_changed_beliefs_only=save_changed_beliefs_only
    )
    db.session.commit()

    # Only enqueue forecasting jobs upon successfully saving new data
    if status[:7] == "success" and status != "success_but_nothing_new":
        enqueue_forecasting_jobs(forecasting_jobs)

    # Pick a response
    if status == "success":
        return request_processed()
    elif status in (
        "success_with_unchanged_beliefs_skipped",
        "success_but_nothing_new",
    ):
        return already_received_and_successfully_processed()
    return invalid_replacement()


def save_to_db(
    timed_values: Union[BeliefsDataFrame, List[Union[Power, Price, Weather]]],
    forecasting_jobs: List[Job] = [],
    save_changed_beliefs_only: bool = True,
) -> ResponseTuple:
    """Put the timed values into the database and enqueue forecasting jobs.

    Data can only be replaced on servers in play mode.

    TODO: remove this legacy function in its entirety (announced v0.8.0)

    :param timed_values: BeliefsDataFrame or a list of Power, Price or Weather values to be saved
    :param forecasting_jobs: list of forecasting Jobs for redis queues.
    :param save_changed_beliefs_only: if True, beliefs that are already stored in the database with an earlier belief time are dropped.
    :returns: ResponseTuple
    """

    import warnings

    warnings.warn(
        "The method api.common.utils.api_utils.save_to_db is deprecated. Check out the following replacements:"
        "- [recommended option] to store BeliefsDataFrames only, switch to data.utils.save_to_db"
        "- to store BeliefsDataFrames and enqueue jobs, switch to api.common.utils.api_utils.save_and_enqueue"
    )

    if isinstance(timed_values, BeliefsDataFrame):

        if save_changed_beliefs_only:
            # Drop beliefs that haven't changed
            timed_values = (
                timed_values.convert_index_from_belief_horizon_to_time()
                .groupby(level=["belief_time", "source"], as_index=False)
                .apply(drop_unchanged_beliefs)
            )

            # Work around bug in which groupby still introduces an index level, even though we asked it not to
            if None in timed_values.index.names:
                timed_values.index = timed_values.index.droplevel(None)

        if timed_values.empty:
            current_app.logger.debug("Nothing new to save")
            return already_received_and_successfully_processed()

    current_app.logger.info("SAVING TO DB AND QUEUEING...")
    try:
        if isinstance(timed_values, BeliefsDataFrame):
            TimedBelief.add_to_session(
                session=db.session, beliefs_data_frame=timed_values
            )
        else:
            save_to_session(timed_values)
        db.session.flush()
        [current_app.queues["forecasting"].enqueue_job(job) for job in forecasting_jobs]
        db.session.commit()
        return request_processed()
    except IntegrityError as e:
        current_app.logger.warning(e)
        db.session.rollback()

        # Possibly allow data to be replaced depending on config setting
        if current_app.config.get("FLEXMEASURES_ALLOW_DATA_OVERWRITE", False):
            if isinstance(timed_values, BeliefsDataFrame):
                TimedBelief.add_to_session(
                    session=db.session,
                    beliefs_data_frame=timed_values,
                    allow_overwrite=True,
                )
            else:
                save_to_session(timed_values, overwrite=True)
            [
                current_app.queues["forecasting"].enqueue_job(job)
                for job in forecasting_jobs
            ]
            db.session.commit()
            return request_processed()
        else:
            return already_received_and_successfully_processed()


def determine_belief_timing(
    event_values: list,
    start: datetime,
    resolution: timedelta,
    horizon: timedelta,
    prior: datetime,
    sensor: tb.Sensor,
) -> Tuple[List[datetime], List[timedelta]]:
    """Determine event starts from start, resolution and len(event_values),
    and belief horizons from horizon, prior, or both, taking into account
    the sensor's knowledge horizon function.

    In case both horizon and prior is set, we take the greatest belief horizon,
    which represents the earliest belief time.
    """
    event_starts = [start + j * resolution for j in range(len(event_values))]
    belief_horizons_from_horizon = None
    belief_horizons_from_prior = None
    if horizon is not None:
        belief_horizons_from_horizon = [horizon] * len(event_values)
        if prior is None:
            return event_starts, belief_horizons_from_horizon
    if prior is not None:
        belief_horizons_from_prior = [
            event_start - prior - sensor.knowledge_horizon(event_start)
            for event_start in event_starts
        ]
        if horizon is None:
            return event_starts, belief_horizons_from_prior
    if (
        belief_horizons_from_horizon is not None
        and belief_horizons_from_prior is not None
    ):
        belief_horizons = [
            max(a, b)
            for a, b in zip(belief_horizons_from_horizon, belief_horizons_from_prior)
        ]
        return event_starts, belief_horizons
    raise ValueError("Missing horizon or prior.")


def catch_timed_belief_replacements(error: IntegrityError):
    """Catch IntegrityErrors due to a UniqueViolation on the TimedBelief primary key.

    Return a more informative message.
    """
    if isinstance(error.orig, UniqueViolation) and "timed_belief_pkey" in str(
        error.orig
    ):
        # Some beliefs represented replacements, which was forbidden
        return invalid_replacement()

    # Forward to our generic error handler
    return error_handling_router(error)
