from mindsdb.utilities import log

logger = log.getLogger("mindsdb")
logger.debug("Starting MindsDB...")

import os
import sys
import time
import json
import atexit
import signal
import psutil
import asyncio
import secrets
import traceback
import threading
from textwrap import dedent
from packaging import version

from mindsdb.__about__ import __version__ as mindsdb_version
from mindsdb.api.http.start import start as start_http
from mindsdb.api.mysql.start import start as start_mysql
from mindsdb.api.mongo.start import start as start_mongo
from mindsdb.api.postgres.start import start as start_postgres
from mindsdb.interfaces.tasks.task_monitor import start as start_tasks
from mindsdb.interfaces.jobs.scheduler import start as start_scheduler
from mindsdb.utilities.ml_task_queue.consumer import start as start_ml_task_queue
from mindsdb.utilities.config import Config
from mindsdb.utilities.ps import is_pid_listen_port, get_child_pids
from mindsdb.utilities.functions import args_parse, get_versions_where_predictors_become_obsolete
from mindsdb.utilities.context import context as ctx
from mindsdb.utilities.auth import register_oauth_client, get_aws_meta_data

try:
    import torch.multiprocessing as mp
except Exception:
    import multiprocessing as mp
try:
    mp.set_start_method('spawn')
except RuntimeError:
    logger.info('Torch multiprocessing context already set, ignoring...')


_stop_event = threading.Event()


def close_api_gracefully(apis):
    _stop_event.set()
    try:
        for api in apis.values():
            try:
                process = api['process']
                childs = get_child_pids(process.pid)
                for p in childs:
                    try:
                        os.kill(p, signal.SIGTERM)
                    except Exception:
                        p.kill()
                sys.stdout.flush()
                process.terminate()
                process.join()
                sys.stdout.flush()
            except psutil.NoSuchProcess:
                pass
    except KeyboardInterrupt:
        sys.exit(0)


def do_clean_process_marks():
    while _stop_event.wait(timeout=5) is False:
        clean_unlinked_process_marks()


if __name__ == '__main__':
    # warn if less than 1Gb of free RAM
    if psutil.virtual_memory().available < (1 << 30):
        logger.warning(
            'The system is running low on memory. '
            + 'This may impact the stability and performance of the program.'
        )

    clean_process_marks()
    ctx.set_default()
    args = args_parse()

    # ---- CHECK SYSTEM ----
    if not (sys.version_info[0] >= 3 and sys.version_info[1] >= 8):
        print("""
     MindsDB requires Python >= 3.8 to run

     Once you have Python 3.8 installed you can tun mindsdb as follows:

     1. create and activate venv:
     python3.8 -m venv venv
     source venv/bin/activate

     2. install MindsDB:
     pip3 install mindsdb

     3. Run MindsDB
     python3.8 -m mindsdb

     More instructions in https://docs.mindsdb.com
         """)
        exit(1)

    # --- VERSION MODE ----
    if args is not None and args.version:
        print(f'MindsDB {mindsdb_version}')
        sys.exit(0)

    # --- MODULE OR LIBRARY IMPORT MODE ----
    if args is not None and args.config is not None:
        config_path = args.config
        with open(config_path, 'r') as fp:
            user_config = json.load(fp)
    else:
        user_config = {}
        config_path = 'absent'
    os.environ['MINDSDB_CONFIG_PATH'] = config_path

    config = Config()
    create_dirs_recursive(config['paths'])

    if telemetry_file_exists(config['paths']['telemetry_file']):
        disable_telemetry(config['paths']['telemetry_file'])

    if config.get("jobs", {}).get("disable") is not True:
        apis["jobs"] = {
            'process': None,
            'started': False
        }

    # disabled on cloud
    if config.get('tasks', {}).get('disable') is not True:
        apis['tasks'] = {
            'process': None,
            'started': False
        }

    if args.ml_task_queue_consumer is True:
        apis['ml_task_queue'] = {
            'process': None,
            'started': False
        }

    # TODO this 'ctx' is eclipsing 'context' class imported as 'ctx'
    ctx = mp.get_context("spawn")
    for api_name, api_data in apis.items():
        if api_data["started"]:
            continue
        logger.info(f"{api_name} API: starting...")
        try:
            process_args = (args.verbose,)
            if api_name == 'http':
                process_args = (args.verbose, args.no_studio)
            p = ctx.Process(target=start_functions[api_name], args=process_args, name=api_name)
            p.start()
            api_data["process"] = p
        except Exception as e:
            logger.error(
                f"Failed to start {api_name} API with exception {e}\n{traceback.format_exc()}"
            )
            close_api_gracefully(apis)
            raise e

    atexit.register(close_api_gracefully, apis=apis)

    async def wait_api_start(api_name, pid, port):
        timeout = 60
        start_time = time.time()
        started = is_pid_listen_port(pid, port)
        while (time.time() - start_time) < timeout and started is False:
            await asyncio.sleep(0.5)
            started = is_pid_listen_port(pid, port)
        return api_name, port, started

    async def wait_apis_start():
        futures = [
            wait_api_start(api_name, api_data["process"].pid, api_data["port"])
            for api_name, api_data in apis.items()
            if "port" in api_data
        ]
        for i, future in enumerate(asyncio.as_completed(futures)):
            api_name, port, started = await future
            if started:
                logger.info(f"{api_name} API: started on {port}")
            else:
                logger.error(f"ERROR: {api_name} API cant start on {port}")

    async def join_process(process, name):
        try:
            while process.is_alive():
                process.join(1)
                await asyncio.sleep(0)
        except KeyboardInterrupt:
            logger.info("Got keyboard interrupt, stopping APIs")
            close_api_gracefully(apis)
        finally:
            logger.info(f"{name} API: stopped")

    async def gather_apis():
        await asyncio.gather(
            *[join_process(api_data['process'], api_name) for api_name, api_data in apis.items()],
            return_exceptions=False
        )

    ioloop = asyncio.new_event_loop()
    ioloop.run_until_complete(wait_apis_start())

    threading.Thread(target=do_clean_process_marks).start()

    ioloop.run_until_complete(gather_apis())
    ioloop.close()

