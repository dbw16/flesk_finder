import csv
import datetime
import zipfile
import io

from dateutil import parser
import boto3
from boto3.dynamodb.conditions import Key
import requests
from decimal import *
import logging
from botocore.exceptions import ClientError
import sys

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
log_handler = logging.StreamHandler(sys.stdout)
log_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
log_handler.setFormatter(formatter)
LOGGER.addHandler(log_handler)

dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table("river_levels")

FLESK_GAUGE_NUMBER = 22039
FLESK_PAST_DATA = "https://epawebapp.epa.ie/Hydronet/output/internet/stations/LIM/22039/S/complete_15min.zip"

LAST_N_READINGS = 100


class Level:
    def __init__(self, time: datetime.datetime = 0, level: Decimal = 1):
        self.time = time
        self.level = level


def update_level_db(level: Level) -> bool:
    try:
        table.put_item(
            Item={
                'river_name': "Flesk",
                'timestamp': int(level.time.timestamp()),
                'level': level.level,
            },
            ConditionExpression='attribute_not_exists(river_name)'

        )
    except ClientError as e:
        # Ignore the ConditionalCheckFailedException, bubble up other exceptions.
        if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return False
    return True


def batch_update_level_db(river_name: str, levels: [Level]):
    with table.batch_writer(overwrite_by_pkeys=['river_name', 'timestamp']) as batch:
        for i, level in enumerate(levels):
            print(i)
            batch.put_item(
                Item={
                    'river_name': river_name,
                    'timestamp': int(level.time.timestamp()),
                    'level': level.level,
                }

            )


def get_latest_level(gauge_number: int) -> Level:
    response = requests.get("https://epawebapp.epa.ie/Hydronet/output/internet/layers/10/index.json")
    gauges = response.json()
    for gauge in gauges:
        if gauge['metadata_station_no'] == str(gauge_number):
            parsed_time = parser.parse(gauge['L1_timestamp'])
            return Level(
                level=Decimal(gauge['L1_ts_value']) - Decimal(gauge['L1_station_gauge_datum']),
                time=parsed_time
            )
    print("could not find gauge...")


def get_past_data_epa(n_last_readings: int) -> [Level]:
    response = requests.get(FLESK_PAST_DATA, stream=True)
    z = zipfile.ZipFile(io.BytesIO(response.content))
    z.extract("complete_15min.csv")

    with open("complete_15min.csv") as f:
        csv_reader = csv.reader(f, delimiter=' ', )
        rows = [row for row in csv_reader if "#" not in row[0]]

    levels = []

    for row in rows[- n_last_readings:]:
        date = row[0]
        time = row[1].split(";")[0]
        parsed_time = datetime.datetime(
            year=int(date.split("-")[0]),
            month=int(date.split("-")[1]),
            day=int(date.split("-")[2]),
            hour=int(time.split(":")[0]),
            minute=int(time.split(":")[1])
        )

        try:
            levels.append(Level(time=parsed_time, level=Decimal(row[1].split(";")[1])))
        except:
            pass

    return levels


def get_past_data_dynamo(river_name: str, since_date: datetime.datetime) -> [Level]:
    since_timestamp = int(since_date.timestamp())

    response = table.query(
        KeyConditionExpression=Key('river_name').eq(river_name) & Key('timestamp').gt(since_timestamp)
    )

    return [Level(time=datetime.datetime.fromtimestamp(int(item['timestamp'])), level=item['level']) for item in
            response['Items']]


def get_most_recent_data_dynamo(river_name: str) -> [Level]:
    response = table.query(
        ExpressionAttributeValues=Key('river_name').eq(river_name)
    )
    return [Level(time=datetime.datetime.fromtimestamp(int(item['timestamp'])), level=item['level']) for item in
            response['Items']]


def draw_graph_levels(levels: [Level], river_name: str, low_water, high_water) -> str:
    from bokeh.models.widgets import DateRangeSlider
    from bokeh.layouts import layout
    from bokeh.plotting import figure, output_file, save, show
    from bokeh.embed import file_html
    from bokeh.resources import CDN

    now = datetime.datetime.now()
    two_weeks_ago = now - datetime.timedelta(days=14)

    p = figure(title=f"{river_name} Gauge, current level: {levels[-1].level}m at {levels[-1].time}",
               x_axis_type="datetime", x_axis_label='Date',
               y_axis_label='Height', x_range=(two_weeks_ago, now))
    p.line(x=[l.time for l in levels], y=[l.level for l in levels], legend_label="Level", line_width=2)

    p.line(x=[l.time for l in levels], y=[low_water for _ in levels], legend_label="Low Water", line_color="green",
           line_width=1)
    p.line(x=[l.time for l in levels], y=[high_water for _ in levels], legend_label="High Water", line_color="red",
           line_width=1)

    date_range_slider = DateRangeSlider(
        title="Date Range",
        start=levels[0].time,
        end=datetime.datetime.now(),
        value=(two_weeks_ago, datetime.datetime.now()),
        step=1
    )

    date_range_slider.js_link("value", p.x_range, "start", attr_selector=0)
    date_range_slider.js_link("value", p.x_range, "end", attr_selector=1)

    layout = layout(
        [
            [p],
            [date_range_slider]

        ],
        sizing_mode='stretch_width'
    )

    return file_html(layout, CDN, river_name)


def update_current_levels_table_handler(event, context) -> bool:
    LOGGER.info("Running update_current_levels_table_handler")
    return update_level_db(get_latest_level(FLESK_GAUGE_NUMBER))


def update_past_levels_table_handler(event, context) -> bool:
    LOGGER.info("Running update_past_levels_table_handler")

    # get the newest N river levels from the epa .zip file
    epa_levels = get_past_data_epa(3000)

    # get the most resent river levels in our table
    dynamo_levels = get_past_data_dynamo("Flesk", datetime.datetime.now() - datetime.timedelta(days=50))
    dynamo_times = {level.time for level in dynamo_levels}

    # to save doing extra work we only try to write new level readings to db
    new_river_levels = [level for level in epa_levels if level.time not in dynamo_times]

    if new_river_levels:
        LOGGER.info("Updating db with new epa.zip data ")
        batch_update_level_db("Flesk", new_river_levels)
        return True
    else:
        LOGGER.info("No new data from epa.zip")
        return False


def create_graph_handler(event, context):
    LOGGER.info("Running create_graph_handler")
    levels = get_past_data_dynamo("Flesk", datetime.datetime.now() - datetime.timedelta(days=50))
    draw_graph_levels(levels, "Flesk - Last two weeks", 0.7, 1.5)


def build_website():
    rivers = ["Flesk", ]
    html = []
    for river in rivers:
        levels = get_past_data_dynamo(river, datetime.datetime.now() - datetime.timedelta(days=50))
        river_html = draw_graph_levels(levels, river, 0.7, 1.5)
        html.append(river_html)

    from paramiko import SSHClient
    from scp import SCPClient

    ssh = SSHClient()
    ssh.load_system_host_keys()
    ssh.connect(hostname='salmon.maths.tcd.ie', username="dawillia")

    # SCPCLient takes a paramiko transport as an argument
    scp = SCPClient(ssh.get_transport())

    with open("index.php", "w") as f:
        f.write("\n".join(html))

    scp.put('index.php', remote_path="www/")
    ssh.exec_command("chmod 644 www/index.php")
    scp.close()


def handler(event, context):
    LOGGER.info(event)
    if "current" in event:
        LOGGER.info("current event")
        if update_current_levels_table_handler(event, context):
            LOGGER.info("updated db with new value, making new graph")
            create_graph_handler(event, context)
        else:
            LOGGER.info("no new value found")
    elif "past" in event:
        LOGGER.info("past event")
        if update_past_levels_table_handler(event, context):
            LOGGER.info("updated db with new values, making new graph")
            create_graph_handler(event, context)
        else:
            LOGGER.info("no new values found")
    else:
        LOGGER.info("did nothing, could not parse event payload")


def main():
    # For running locally
    print("Starting: main")
    update_current_levels_table_handler(0, 0)
    update_past_levels_table_handler(0, 0)
    build_website()
    print("done")


if __name__ == '__main__':
    main()
