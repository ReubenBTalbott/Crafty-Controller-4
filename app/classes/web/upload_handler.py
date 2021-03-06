import logging
import os
import time
import tornado.web
import tornado.options
import tornado.httpserver

from app.classes.models.server_permissions import EnumPermissionsServer
from app.classes.shared.console import Console
from app.classes.shared.helpers import Helpers
from app.classes.shared.main_controller import Controller
from app.classes.web.base_handler import BaseHandler

logger = logging.getLogger(__name__)


@tornado.web.stream_request_body
class UploadHandler(BaseHandler):

    # noinspection PyAttributeOutsideInit
    def initialize(
        self,
        helper: Helpers = None,
        controller: Controller = None,
        tasks_manager=None,
        translator=None,
    ):
        self.helper = helper
        self.controller = controller
        self.tasks_manager = tasks_manager
        self.translator = translator

    def prepare(self):
        # Class & Function Defination
        api_key, _token_data, exec_user = self.current_user
        server_id = self.get_argument("server_id", None)
        superuser = exec_user["superuser"]
        if api_key is not None:
            superuser = superuser and api_key.superuser
        user_id = exec_user["user_id"]
        stream_size_value = self.helper.get_setting("stream_size_GB")

        max_streamed_size = (1024 * 1024 * 1024) * stream_size_value

        self.content_len = int(self.request.headers.get("Content-Length"))
        if self.content_len > max_streamed_size:
            logger.error(
                f"User with ID {user_id} attempted to upload a file that"
                f" exceeded the max body size."
            )
            self.helper.websocket_helper.broadcast_user(
                user_id,
                "send_start_error",
                {
                    "error": self.helper.translation.translate(
                        "error",
                        "fileTooLarge",
                        self.controller.users.get_user_lang_by_id(user_id),
                    ),
                },
            )
            return
        self.do_upload = True

        if superuser:
            exec_user_server_permissions = (
                self.controller.server_perms.list_defined_permissions()
            )
        elif api_key is not None:
            exec_user_server_permissions = (
                self.controller.server_perms.get_api_key_permissions_list(
                    api_key, server_id
                )
            )
        else:
            exec_user_server_permissions = (
                self.controller.server_perms.get_user_id_permissions_list(
                    exec_user["user_id"], server_id
                )
            )

        server_id = self.request.headers.get("X-ServerId", None)

        if user_id is None:
            logger.warning("User ID not found in upload handler call")
            Console.warning("User ID not found in upload handler call")
            self.do_upload = False

        if server_id is None:
            logger.warning("Server ID not found in upload handler call")
            Console.warning("Server ID not found in upload handler call")
            self.do_upload = False

        if EnumPermissionsServer.FILES not in exec_user_server_permissions:
            logger.warning(
                f"User {user_id} tried to upload a file to "
                f"{server_id} without permissions!"
            )
            Console.warning(
                f"User {user_id} tried to upload a file to "
                f"{server_id} without permissions!"
            )
            self.do_upload = False

        path = self.request.headers.get("X-Path", None)
        filename = self.request.headers.get("X-FileName", None)
        full_path = os.path.join(path, filename)

        if not Helpers.in_path(
            Helpers.get_os_understandable_path(
                self.controller.servers.get_server_data_by_id(server_id)["path"]
            ),
            full_path,
        ):
            print(
                user_id,
                server_id,
                Helpers.get_os_understandable_path(
                    self.controller.servers.get_server_data_by_id(server_id)["path"]
                ),
                full_path,
            )
            logger.warning(
                f"User {user_id} tried to upload a file to {server_id} "
                f"but the path is not inside of the server!"
            )
            Console.warning(
                f"User {user_id} tried to upload a file to {server_id} "
                f"but the path is not inside of the server!"
            )
            self.do_upload = False

        if self.do_upload:
            try:
                self.f = open(full_path, "wb")
            except Exception as e:
                logger.error(f"Upload failed with error: {e}")
                self.do_upload = False
        # If max_body_size is not set, you cannot upload files > 100MB
        self.request.connection.set_max_body_size(max_streamed_size)

    def post(self):
        logger.info("Upload completed")
        files_left = int(self.request.headers.get("X-Files-Left", None))

        if self.do_upload:
            time.sleep(5)
            if files_left == 0:
                self.helper.websocket_helper.broadcast("close_upload_box", "success")
            self.finish("success")  # Nope, I'm sending "success"
            self.f.close()
        else:
            time.sleep(5)
            if files_left == 0:
                self.helper.websocket_helper.broadcast("close_upload_box", "error")
            self.finish("error")

    def data_received(self, chunk):
        if self.do_upload:
            self.f.write(chunk)
