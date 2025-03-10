import argparse
import contextlib
import csv
import datetime
import json
import os

import boto3
from griptape.artifacts import TextArtifact
from griptape.configs import defaults_config
from griptape.configs.drivers import DriversConfig
from griptape.drivers import (
    AnthropicPromptDriver,
    GriptapeCloudEventListenerDriver,
)
from griptape.events import EventBus, EventListener, FinishStructureRunEvent
from griptape.loaders import CsvLoader
from griptape.rules import Rule
from griptape.structures import Agent
from griptape.tasks import PromptTask


def is_running_in_managed_environment() -> bool:
    return "GT_CLOUD_STRUCTURE_RUN_ID" in os.environ


def get_listener_api_key() -> str:
    api_key = os.environ.get("GT_CLOUD_API_KEY", "")
    if is_running_in_managed_environment() and not api_key:
        pass
    return api_key


def filter_spreadsheet(filter_by: str, input_file: str) -> list:
    with open(input_file) as file:
        first_line = file.readline().strip()
        headers = first_line.split(",")

    agent = Agent(
        tasks=[
            PromptTask(
                input=f"Return the column names related to {filter_by} in the following data: {headers}",
                rules=[Rule("Output a json list of strings")],
            )
        ],
        rules=[
            Rule("Output JUST the data with NO commentary"),
        ],
    )

    agent.run()
    output_json = agent.output.to_text()
    column_names = json.loads(output_json)

    with open(input_file) as file:
        reader = csv.DictReader(file)
        return [{col: row[col] for col in column_names if col in row} for row in reader]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-i",
        "--input_file",
        required=True,
        help="S3 URI to input spreadsheet",
    )
    parser.add_argument(
        "-d",
        "--data_to_parse",
        required=True,
        help="the prompt data to parse out of the CSV",
    )
    parser.add_argument(
        "-o",
        "--output_file_name",
        help="name of the output file to write. It will write to the same S3 bucket in a sub-folder called 'output'.",
        default="csv_filter_output",
    )

    args = parser.parse_args()
    if is_running_in_managed_environment():
        event_driver = GriptapeCloudEventListenerDriver(api_key=get_listener_api_key())
    else:
        from dotenv import load_dotenv

        load_dotenv()
        event_driver = None

    defaults_config.Defaults.drivers_config = DriversConfig(
        prompt_driver=AnthropicPromptDriver(
            model="claude-3-5-sonnet-20240620",
            api_key=os.environ["ANTHROPIC_API_KEY"],
            max_tokens=8192,
            temperature=0.0,
        )
    )

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=str(os.environ.get("AWS_ACCESS_KEY_ID")),
        aws_secret_access_key=str(os.environ.get("AWS_SECRET_ACCESS_KEY")),
    )

    bucket, key = args.input_file.split("/", 2)[-1].split("/", 1)

    input_file_local = f"downloaded/{key}"
    os.makedirs(os.path.dirname(input_file_local), exist_ok=True)
    s3_client.download_file(bucket, key, input_file_local)

    output_file_name = args.output_file_name
    output_file_path_local = (
        f"output/{output_file_name}-"
        + str(datetime.datetime.now(tz=datetime.UTC).strftime("%Y.%m.%d-%H:%M:%S"))
        + ".csv"
    )

    extracted_data = filter_spreadsheet(args.data_to_parse, input_file_local)

    os.makedirs(os.path.dirname(output_file_path_local), exist_ok=True)
    with open(output_file_path_local, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=extracted_data[0].keys())
        writer.writeheader()
        writer.writerows(extracted_data)

    with contextlib.suppress(Exception):
        s3_client.upload_file(output_file_path_local, bucket, output_file_path_local)

    # This code is if you run this Structure as a GTC DC
    if event_driver is not None:
        artifacts = CsvLoader().load(output_file_path_local)

        task_input = TextArtifact(value=None)
        done_event = FinishStructureRunEvent(output_task_input=task_input, output_task_output=artifacts)

        EventBus.add_event_listener(EventListener(event_listener_driver=event_driver))
        EventBus.publish_event(done_event, flush=True)
