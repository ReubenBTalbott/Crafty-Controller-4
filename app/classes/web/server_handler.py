import sys
import json
import logging
import os
import shutil
import libgravatar
import requests

from app.classes.shared.helpers import helper
from app.classes.shared.console import console
from app.classes.web.base_handler import BaseHandler
from app.classes.models.crafty_permissions import Enum_Permissions_Crafty
from app.classes.minecraft.serverjars import server_jar_obj


logger = logging.getLogger(__name__)

try:
    import tornado.web
    import tornado.escape
    import bleach

except ModuleNotFoundError as e:
    logger.critical(f"Import Error: Unable to load {e.name} module", exc_info=True)
    console.critical(f"Import Error: Unable to load {e.name} module")
    sys.exit(1)


class ServerHandler(BaseHandler):

    @tornado.web.authenticated
    def get(self, page):
        # pylint: disable=unused-variable
        api_key, token_data, exec_user = self.current_user
        superuser = exec_user['superuser']
        if api_key is not None:
            superuser = superuser and api_key.superuser

        exec_user_role = set()
        if superuser:
            defined_servers = self.controller.list_defined_servers()
            exec_user_role.add("Super User")
            exec_user_crafty_permissions = self.controller.crafty_perms.list_defined_crafty_permissions()
            list_roles = []
            for role in self.controller.roles.get_all_roles():
                list_roles.append(self.controller.roles.get_role(role.role_id))
        else:
            exec_user_crafty_permissions = self.controller.crafty_perms.get_crafty_permissions_list(exec_user["user_id"])
            defined_servers = self.controller.servers.get_authorized_servers(exec_user["user_id"])
            list_roles = []
            for r in exec_user['roles']:
                role = self.controller.roles.get_role(r)
                exec_user_role.add(role['role_name'])
                list_roles.append(self.controller.roles.get_role(role['role_id']))

        template = "public/404.html"

        page_data = {
            'version_data': helper.get_version_string(),
            'user_data': exec_user,
            'user_role' : exec_user_role,
            'roles' : list_roles,
            'user_crafty_permissions' : exec_user_crafty_permissions,
            'crafty_permissions': {
                'Server_Creation': Enum_Permissions_Crafty.Server_Creation,
                'User_Config': Enum_Permissions_Crafty.User_Config,
                'Roles_Config': Enum_Permissions_Crafty.Roles_Config,
            },
            'server_stats': {
                'total': len(self.controller.list_defined_servers()),
                'running': len(self.controller.list_running_servers()),
                'stopped': (len(self.controller.list_defined_servers()) - len(self.controller.list_running_servers()))
            },
            'hosts_data': self.controller.management.get_latest_hosts_stats(),
            'menu_servers': defined_servers,
            'show_contribute': helper.get_setting("show_contribute_link", True),
            'lang': self.controller.users.get_user_lang_by_id(exec_user["user_id"]),
            'api_key': {
                'name': api_key.name,
                'created': api_key.created,
                'server_permissions': api_key.server_permissions,
                'crafty_permissions': api_key.crafty_permissions,
                'superuser': api_key.superuser
            } if api_key is not None else None,
            'superuser': superuser
        }
        if  helper.get_setting("allow_nsfw_profile_pictures"):
            rating = "x"
        else:
            rating = "g"


        if exec_user['email'] != 'default@example.com' or "":
            g = libgravatar.Gravatar(libgravatar.sanitize_email(exec_user['email']))
            url = g.get_image(size=80, default="404", force_default=False, rating=rating, filetype_extension=False, use_ssl=True) # + "?d=404"
            if requests.head(url).status_code != 404:
                profile_url = url
            else:
                profile_url = "/static/assets/images/faces-clipart/pic-3.png"
        else:
            profile_url = "/static/assets/images/faces-clipart/pic-3.png"

        page_data['user_image'] = profile_url
        if superuser:
            page_data['roles'] = list_roles

        if page == "step1":
            if not superuser and not self.controller.crafty_perms.can_create_server(exec_user["user_id"]):
                self.redirect("/panel/error?error=Unauthorized access: not a server creator or server limit reached")
                return

            page_data['server_types'] = server_jar_obj.get_serverjar_data()
            page_data['js_server_types'] = json.dumps(server_jar_obj.get_serverjar_data())
            template = "server/wizard.html"

        if page == "bedrock_step1":
            if not superuser and not self.controller.crafty_perms.can_create_server(exec_user["user_id"]):
                self.redirect("/panel/error?error=Unauthorized access: not a server creator or server limit reached")
                return

            template = "server/bedrock_wizard.html"

        self.render(
            template,
            data=page_data,
            translate=self.translator.translate,
        )

    @tornado.web.authenticated
    def post(self, page):
        # pylint: disable=unused-variable
        api_key, token_data, exec_user = self.current_user
        superuser = exec_user['superuser']
        if api_key is not None:
            superuser = superuser and api_key.superuser

        template = "public/404.html"
        page_data = {
            'version_data': "version_data_here", # TODO
            'user_data': exec_user,
            'show_contribute': helper.get_setting("show_contribute_link", True),
            'lang': self.controller.users.get_user_lang_by_id(exec_user["user_id"])
        }

        if page == "command":
            server_id = bleach.clean(self.get_argument("id", None))
            command = bleach.clean(self.get_argument("command", None))

            if server_id is not None:
                if command == "clone_server":
                    def is_name_used(name):
                        for server in self.controller.servers.get_all_defined_servers():
                            if server['server_name'] == name:
                                return True
                        return

                    server_data = self.controller.servers.get_server_data_by_id(server_id)
                    server_uuid = server_data.get('server_uuid')
                    new_server_name = server_data.get('server_name') + " (Copy)"

                    name_counter = 1
                    while is_name_used(new_server_name):
                        name_counter += 1
                        new_server_name = server_data.get('server_name') + f" (Copy {name_counter})"

                    new_server_uuid = helper.create_uuid()
                    while os.path.exists(os.path.join(helper.servers_dir, new_server_uuid)):
                        new_server_uuid = helper.create_uuid()
                    new_server_path = os.path.join(helper.servers_dir, new_server_uuid)

                    # copy the old server
                    shutil.copytree(server_data.get('path'), new_server_path)

                    # TODO get old server DB data to individual variables
                    stop_command = server_data.get('stop_command')
                    new_server_command = str(server_data.get('execution_command')).replace(server_uuid, new_server_uuid)
                    new_executable = server_data.get('executable')
                    new_server_log_file = str(helper.get_os_understandable_path(server_data.get('log_path'))).replace(server_uuid, new_server_uuid)
                    server_port = server_data.get('server_port')
                    server_type = server_data.get('server_type')

                    self.controller.servers.create_server(new_server_name,
                                                          new_server_uuid,
                                                          new_server_path,
                                                          "",
                                                          new_server_command,
                                                          new_executable,
                                                          new_server_log_file,
                                                          stop_command,
                                                          server_type,
                                                          server_port)

                    self.controller.init_all_servers()

                    return

                self.controller.management.send_command(exec_user['user_id'], server_id, self.get_remote_ip(), command)

        if page == "step1":

            if not superuser:
                user_roles = self.controller.roles.get_all_roles()
            else:
                user_roles = self.controller.roles.get_all_roles()
            server = bleach.clean(self.get_argument('server', ''))
            server_name = bleach.clean(self.get_argument('server_name', ''))
            min_mem = bleach.clean(self.get_argument('min_memory', ''))
            max_mem = bleach.clean(self.get_argument('max_memory', ''))
            port = bleach.clean(self.get_argument('port', ''))
            import_type = bleach.clean(self.get_argument('create_type', ''))
            import_server_path = bleach.clean(self.get_argument('server_path', ''))
            import_server_jar = bleach.clean(self.get_argument('server_jar', ''))
            server_parts = server.split("|")
            captured_roles = []
            for role in user_roles:
                if bleach.clean(self.get_argument(str(role), '')) == "on":
                    captured_roles.append(role)

            if not server_name:
                self.redirect("/panel/error?error=Server name cannot be empty!")
                return

            if import_type == 'import_jar':
                good_path = self.controller.verify_jar_server(import_server_path, import_server_jar)

                if not good_path:
                    self.redirect("/panel/error?error=Server path or Server Jar not found!")
                    return

                new_server_id = self.controller.import_jar_server(server_name, import_server_path,import_server_jar, min_mem, max_mem, port)
                self.controller.management.add_to_audit_log(exec_user['user_id'],
                                           f"imported a jar server named \"{server_name}\"", # Example: Admin imported a server named "old creative"
                                           new_server_id,
                                           self.get_remote_ip())
            elif import_type == 'import_zip':
                # here import_server_path means the zip path
                zip_path = bleach.clean(self.get_argument('root_path'))
                good_path = helper.check_path_exists(zip_path)
                if not good_path:
                    self.redirect("/panel/error?error=Temp path not found!")
                    return

                new_server_id = self.controller.import_zip_server(server_name, zip_path, import_server_jar, min_mem, max_mem, port)
                if new_server_id == "false":
                    self.redirect("/panel/error?error=Zip file not accessible! You can fix this permissions issue with" +
                                  f"sudo chown -R crafty:crafty {import_server_path} And sudo chmod 2775 -R {import_server_path}")
                    return
                self.controller.management.add_to_audit_log(exec_user['user_id'],
                                           f"imported a zip server named \"{server_name}\"", # Example: Admin imported a server named "old creative"
                                           new_server_id,
                                           self.get_remote_ip())
                #deletes temp dir
                shutil.rmtree(zip_path)
            else:
                if len(server_parts) != 2:
                    self.redirect("/panel/error?error=Invalid server data")
                    return
                server_type, server_version = server_parts
                # TODO: add server type check here and call the correct server add functions if not a jar
                role_ids = self.controller.users.get_user_roles_id(exec_user["user_id"])
                new_server_id = self.controller.create_jar_server(server_type, server_version, server_name, min_mem, max_mem, port)
                self.controller.management.add_to_audit_log(exec_user['user_id'],
                                           f"created a {server_version} {str(server_type).capitalize()} server named \"{server_name}\"",
                                           # Example: Admin created a 1.16.5 Bukkit server named "survival"
                                           new_server_id,
                                           self.get_remote_ip())

            # These lines create a new Role for the Server with full permissions and add the user to it if he's not a superuser
            if len(captured_roles) == 0:
                if not superuser:
                    new_server_uuid = self.controller.servers.get_server_data_by_id(new_server_id).get("server_uuid")
                    role_id = self.controller.roles.add_role(f"Creator of Server with uuid={new_server_uuid}")
                    self.controller.server_perms.add_role_server(new_server_id, role_id, "11111111")
                    self.controller.users.add_role_to_user(exec_user["user_id"], role_id)
                    self.controller.crafty_perms.add_server_creation(exec_user["user_id"])

            else:
                for role in captured_roles:
                    role_id = role
                    self.controller.server_perms.add_role_server(new_server_id, role_id, "11111111")

            self.controller.stats.record_stats()
            self.redirect("/panel/dashboard")

        try:
            self.render(
                template,
                data=page_data,
                translate=self.translator.translate,
            )
        except RuntimeError:
            self.redirect('/panel/dashboard')
