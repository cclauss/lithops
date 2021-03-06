import os
import io
import sys
import json
import pkgutil
import logging
import uuid
import time
import queue
import multiprocessing as mp
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from contextlib import redirect_stdout, redirect_stderr

from lithops.utils import version_str, is_unix_system
from lithops.worker import function_handler
from lithops.config import STORAGE_DIR, JOBS_DONE_DIR, FN_LOG_FILE,\
    LH_LOG_FILE, default_logging_config
from lithops import __version__

os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(JOBS_DONE_DIR, exist_ok=True)

logging.basicConfig(filename=LH_LOG_FILE, level=logging.INFO,
                    format=('%(asctime)s [%(levelname)s] '
                            '%(module)s: %(message)s'))
logger = logging.getLogger('handler')

CPU_COUNT = mp.cpu_count()


def extract_runtime_meta():
    runtime_meta = dict()
    mods = list(pkgutil.iter_modules())
    runtime_meta["preinstalls"] = [entry for entry in sorted([[mod, is_pkg]for _, mod, is_pkg in mods])]
    runtime_meta["python_ver"] = version_str(sys.version_info)

    print(json.dumps(runtime_meta))


class ShutdownSentinel():
    """Put an instance of this class on the queue to shut it down"""
    pass


class LocalhostExecutor:
    """
    A wrap-up around Localhost multiprocessing APIs.
    """

    def __init__(self, config):
        self.config = config
        self.use_threads = not is_unix_system()
        self.num_workers = self.config['lithops'].get('workers', CPU_COUNT)
        self.workers = []

        log_file_stream = open(LH_LOG_FILE, 'a')
        sys.stdout = log_file_stream
        sys.stderr = log_file_stream

        if self.use_threads:
            self.queue = queue.Queue()
            for worker_id in range(self.num_workers):
                p = Thread(target=self._process_runner, args=(worker_id,))
                self.workers.append(p)
                p.start()
        else:
            self.queue = mp.Queue()
            for worker_id in range(self.num_workers):
                p = mp.Process(target=self._process_runner, args=(worker_id,))
                self.workers.append(p)
                p.start()

        logger.info('ExecutorID {} | JobID {} - Localhost Executor started - {} workers'
                    .format(job.executor_id, job.job_id, self.num_workers))

    def _process_runner(self, worker_id):
        logger.debug('Localhost worker process {} started'.format(worker_id))

        while True:
            with io.StringIO() as buf, redirect_stdout(buf), redirect_stderr(buf):
                try:
                    act_id = str(uuid.uuid4()).replace('-', '')[:12]
                    os.environ['__LITHOPS_ACTIVATION_ID'] = act_id

                    event = self.queue.get(block=True)
                    if isinstance(event, ShutdownSentinel):
                        break

                    log_level = event['log_level']
                    default_logging_config(log_level)
                    logger.info("Lithops v{} - Starting execution".format(__version__))
                    event['extra_env']['__LITHOPS_LOCAL_EXECUTION'] = 'True'
                    function_handler(event)
                except KeyboardInterrupt:
                    break

                header = "Activation: '{}' ({})\n[\n".format(event['runtime_name'], act_id)
                tail = ']\n\n'
                output = buf.getvalue()
                output = output.replace('\n', '\n    ', output.count('\n')-1)

            with open(FN_LOG_FILE, 'a') as lf:
                lf.write(header+'    '+output+tail)

    def _invoke(self, job, call_id):
        payload = {'config': self.config,
                   'log_level': logging.getLevelName(logger.getEffectiveLevel()),
                   'func_key': job.func_key,
                   'data_key': job.data_key,
                   'extra_env': job.extra_env,
                   'execution_timeout': job.execution_timeout,
                   'data_byte_range': job.data_ranges[int(call_id)],
                   'executor_id': job.executor_id,
                   'job_id': job.job_id,
                   'call_id': call_id,
                   'host_submit_tstamp': time.time(),
                   'lithops_version': __version__,
                   'runtime_name': job.runtime_name,
                   'runtime_memory': job.runtime_memory}

        self.queue.put(payload)

    def run(self, job_description):
        job = SimpleNamespace(**job_description)

        for i in range(job.total_calls):
            call_id = "{:05d}".format(i)
            self._invoke(job, call_id)

        for i in self.workers:
            self.queue.put(ShutdownSentinel())

    def wait(self):
        for worker in self.workers:
            worker.join()


if __name__ == "__main__":
    logger.info('Starting Localhost job handler')
    command = sys.argv[1]
    logger.info('Received command: {}'.format(command))

    if command == 'preinstalls':
        extract_runtime_meta()

    elif command == 'run':
        job_filename = sys.argv[2]
        logger.info('Got {} job file'.format(job_filename))

        with open(job_filename, 'rb') as jf:
            job = SimpleNamespace(**json.load(jf))

        logger.info('ExecutorID {} | JobID {} - Starting execution'
                    .format(job.executor_id, job.job_id))
        localhost_execuor = LocalhostExecutor(job.config)
        localhost_execuor.run(job.job_description)
        localhost_execuor.wait()

        sentinel = '{}/{}_{}.done'.format(JOBS_DONE_DIR,
                                          job.executor_id.replace('/', '-'),
                                          job.job_id)
        Path(sentinel).touch()

        logger.info('ExecutorID {} | JobID {} - Execution Finished'
                    .format(job.executor_id, job.job_id))
