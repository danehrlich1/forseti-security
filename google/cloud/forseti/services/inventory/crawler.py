# Copyright 2017 The Forseti Security Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Crawler implementation."""

# TODO: Remove this when time allows
# pylint: disable=missing-type-doc,missing-return-type-doc,missing-return-doc
# pylint: disable=missing-param-doc

import threading
import time
from Queue import Empty, Queue

from google.cloud.forseti.services.inventory.base import crawler
from google.cloud.forseti.services.inventory.base import gcp
from google.cloud.forseti.services.inventory.base import resources


class CrawlerConfig(crawler.CrawlerConfig):
    """Crawler configuration to inject dependencies."""

    def __init__(self, storage, progresser, api_client, variables=None):
        super(CrawlerConfig, self).__init__()
        self.storage = storage
        self.progresser = progresser
        self.variables = {} if not variables else variables
        self.client = api_client


class ParallelCrawlerConfig(crawler.CrawlerConfig):
    """Multithreaded crawler configuration, to inject dependencies."""

    def __init__(self, storage, progresser, api_client, threads=10,
                 variables=None):
        super(ParallelCrawlerConfig, self).__init__()
        self.storage = storage
        self.progresser = progresser
        self.variables = {} if not variables else variables
        self.threads = threads
        self.client = api_client


class Crawler(crawler.Crawler):
    """Simple single-threaded Crawler implementation."""

    def __init__(self, config):
        super(Crawler, self).__init__()
        self.config = config

    def run(self, resource):
        """Run the crawler, given a start resource.
        Args:
            resource (object): Resource to start with.
        """

        resource.accept(self)
        return self.config.progresser

    def visit(self, resource):
        """Handle a newly found resource.
        Args:
            resource (object): Resource to handle.
        Raises:
            Exception: Reraises any exception.
        """

        progresser = self.config.progresser
        try:

            resource.getIamPolicy(self.get_client())
            resource.getGCSPolicy(self.get_client())
            resource.getDatasetPolicy(self.get_client())
            resource.getCloudSQLPolicy(self.get_client())
            resource.getBillingInfo(self.get_client())
            resource.getEnabledAPIs(self.get_client())

            self.write(resource)
        except Exception as e:
            progresser.on_error(e)
            raise
        else:
            progresser.on_new_object(resource.key())

    def dispatch(self, callback):
        """Dispatch crawling of a subtree.
        Args:
            callback (function): Callback to dispatch.
        """
        callback()

    def write(self, resource):
        """Save resource to storage.

        Args:
            resource (object): Resource to handle.
        """
        self.config.storage.write(resource)

    def get_client(self):
        """Get the GCP API client."""

        return self.config.client

    def on_child_error(self, error):
        """Process the error generated by child of a resource
           Inventory does not stop for children errors but raise a warning
        """

        warning_message = '{}\n'.format(error)
        self.config.storage.warning(warning_message)
        self.config.progresser.on_warning(error)

    def update(self, resource):
        """Update the row of an existing resource
        Raises:
            Exception: Reraises any exception.
        """

        try:
            self.config.storage.update(resource)
        except Exception as e:
            self.config.progresser.on_error(e)
            raise


class ParallelCrawler(Crawler):
    """Multi-threaded Crawler implementation."""

    def __init__(self, config):
        super(ParallelCrawler, self).__init__(config)
        self._write_lock = threading.Lock()
        self._dispatch_queue = Queue()
        self._shutdown_event = threading.Event()

    def _start_workers(self):
        """Start a pool of worker threads for processing the dispatch queue."""
        self._shutdown_event.clear()
        for _ in xrange(self.config.threads):
            worker = threading.Thread(target=self._process_queue)
            worker.daemon = True
            worker.start()

    def _process_queue(self):
        """Process items in the queue until the shutdown event is set."""
        while not self._shutdown_event.is_set():
            try:
                callback = self._dispatch_queue.get(timeout=1)
            except Empty:
                continue

            callback()
            self._dispatch_queue.task_done()

    def run(self, resource):
        """Run the crawler, given a start resource.

        Args:
            resource (object): Resource to start with.
        """
        try:
            self._start_workers()
            resource.accept(self)
            self._dispatch_queue.join()
        finally:
            self._shutdown_event.set()
            # Wait for threads to exit.
            time.sleep(2)
        return self.config.progresser

    def dispatch(self, callback):
        """Dispatch crawling of a subtree.

        Args:
            callback (function): Callback to dispatch.
        """
        self._dispatch_queue.put(callback)

    def write(self, resource):
        """Save resource to storage.

        Args:
            resource (object): Resource to handle.
        """
        with self._write_lock:
            self.config.storage.write(resource)

    def on_child_error(self, error):
        """Process the error generated by child of a resource
           Inventory does not stop for children errors but raise a warning
        """

        warning_message = '{}\n'.format(error)
        with self._write_lock:
            self.config.storage.warning(warning_message)

        self.config.progresser.on_warning(error)

    def update(self, resource):
        """Update the row of an existing resource

        Raises:
            Exception: Reraises any exception.
        """

        try:
            with self._write_lock:
                self.config.storage.update(resource)
        except Exception as e:
            self.config.progresser.on_error(e)
            raise


def run_crawler(storage,
                progresser,
                config,
                parallel=True):
    """Run the crawler with a determined configuration.

    Args:
        storage (object): Storage implementation to use.
        progresser (object): Progresser to notify status updates.
        config (object): Inventory configuration.
        parallel (bool): If true, use the parallel crawler implementation.
    """

    client_config = {
        'groups_service_account_key_file': config.get_gsuite_sa_path(),
        'domain_super_admin_email': config.get_gsuite_admin_email(),
        'max_admin_api_calls_per_100_seconds': 1500,
        'max_appengine_api_calls_per_second': 20,
        'max_bigquery_api_calls_per_100_seconds': 17000,
        'max_cloudbilling_api_calls_per_60_seconds': 300,
        'max_crm_api_calls_per_100_seconds': 400,
        'max_sqladmin_api_calls_per_100_seconds': 100,
        'max_servicemanagement_api_calls_per_100_seconds': 200,
        'max_compute_api_calls_per_second': 20,
        'max_iam_api_calls_per_second': 20,
        }

    root_id = config.get_root_resource_id()
    client = gcp.ApiClientImpl(client_config)
    resource = resources.from_root_id(client, root_id)
    if parallel:
        config = ParallelCrawlerConfig(storage, progresser, client)
        crawler_impl = ParallelCrawler(config)
    else:
        config = CrawlerConfig(storage, progresser, client)
        crawler_impl = Crawler(config)

    progresser = crawler_impl.run(resource)
    return progresser.get_summary()