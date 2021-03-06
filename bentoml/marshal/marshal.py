# Copyright 2019 Atalaya Tech, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from collections import OrderedDict
import asyncio
import logging
import uuid
import aiohttp

from bentoml import config
from bentoml.utils.trace import async_trace, make_http_headers
from bentoml.marshal.utils import merge_aio_requests, split_aio_responses


logger = logging.getLogger(__name__)
ZIPKIN_API_URL = config("tracing").get("zipkin_api_url")


class Parade:
    STATUSES = (STATUS_OPEN, STATUS_CLOSED, STATUS_RETURNED,) = range(3)

    def __init__(self):
        self.batch_input = OrderedDict()
        self.batch_output = None
        self.returned = asyncio.Condition()
        self.status = self.STATUS_OPEN

    def feed(self, id_, data):
        assert self.status == self.STATUS_OPEN
        self.batch_input[id_] = data
        return True

    async def start_wait(self, interval, call):
        try:
            await asyncio.sleep(interval)
            self.status = self.STATUS_CLOSED
            outputs = await call(self.batch_input.values())
            self.batch_output = OrderedDict(
                [(k, v) for k, v in zip(self.batch_input.keys(), outputs)]
            )
            self.status = self.STATUS_RETURNED
            async with self.returned:
                self.returned.notify_all()
        except Exception as e:  # noqa TODO
            raise e
        finally:
            # make sure parade is closed
            self.status = self.STATUS_CLOSED


class ParadeDispatcher:
    def __init__(self, interval):
        '''
        params:
            * interval: milliseconds
        '''
        self.interval = interval
        self.callback = None
        self._current_parade = None

    def get_parade(self):
        if self._current_parade and self._current_parade.status == Parade.STATUS_OPEN:
            return self._current_parade
        self._current_parade = Parade()
        asyncio.get_event_loop().create_task(
            self._current_parade.start_wait(self.interval / 1000.0, self.callback)
        )
        return self._current_parade

    def __call__(self, callback):
        self.callback = callback

        async def _func(inputs):
            id_ = uuid.uuid4().hex
            parade = self.get_parade()
            parade.feed(id_, inputs)
            async with parade.returned:
                await parade.returned.wait()
            return parade.batch_output.get(id_)

        return _func


class MarshalService:
    _MARSHAL_FLAG = config("marshal_server").get("marshal_request_header_flag")

    def __init__(self, target_host="localhost", target_port=None):
        self.target_host = target_host
        self.target_port = target_port
        self.batch_handlers = dict()

    def set_target_port(self, target_port):
        self.target_port = target_port

    def add_batch_handler(self, api_name, max_latency):
        if api_name not in self.batch_handlers:

            @ParadeDispatcher(max_latency)
            async def _func(requests):
                headers = {self._MARSHAL_FLAG: 'true'}
                api_url = f"http://{self.target_host}:{self.target_port}/{api_name}"

                with async_trace(
                    ZIPKIN_API_URL,
                    service_name=self.__class__.__name__,
                    span_name=f"merged {api_name}",
                ) as trace_ctx:
                    headers.update(make_http_headers(trace_ctx))

                    reqs_s = await merge_aio_requests(requests)
                    async with aiohttp.ClientSession() as client:
                        async with client.post(
                            api_url, data=reqs_s, headers=headers
                        ) as resp:
                            resps = await split_aio_responses(resp)
                if resps is None:
                    return [aiohttp.web.HTTPInternalServerError] * len(requests)
                return resps

            self.batch_handlers[api_name] = _func

    async def request_dispatcher(self, request):
        with async_trace(
            ZIPKIN_API_URL,
            request.headers,
            service_name=self.__class__.__name__,
            span_name="handle request",
        ):
            api_name = request.match_info['name']
            if api_name in self.batch_handlers:
                resp = await self.batch_handlers[api_name](request)
                return resp
            else:
                resp = await self._relay_handler(request, api_name)
                return resp

    def make_app(self):
        app = aiohttp.web.Application()
        app.router.add_post('/{name}', self.request_dispatcher)
        return app

    def fork_start_app(self, port):
        # Use new eventloop in the fork process to avoid problems on MacOS
        # ref: https://groups.google.com/forum/#!topic/python-tornado/DkXjSNPCzsI
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        app = self.make_app()
        aiohttp.web.run_app(app, port=port)

    async def _relay_handler(self, request, api_name):
        data = await request.read()
        headers = request.headers
        api_url = f"http://{self.target_host}:{self.target_port}/{api_name}"

        with async_trace(
            ZIPKIN_API_URL,
            service_name=self.__class__.__name__,
            span_name=f"{api_name} relay",
        ) as trace_ctx:
            headers.update(make_http_headers(trace_ctx))
            async with aiohttp.ClientSession() as client:
                async with client.post(
                    api_url, data=data, headers=request.headers
                ) as resp:
                    body = await resp.read()
            return aiohttp.web.Response(
                status=resp.status, body=body, headers=resp.headers,
            )
