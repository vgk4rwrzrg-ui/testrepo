import os

from django.urls import get_script_prefix, set_script_prefix


class PrefixMiddleware:
    """Apply PRE_FIX in JupyterHub; do nothing when it is unset."""

    def __init__(self, get_response):
        self.get_response = get_response
        value = os.environ.get("pre_fix", "").strip()
        print(value)
        self.prefix = f"/{value.strip('/')}" if value else ""

    def __call__(self, request):
        if not self.prefix:
            return self.get_response(request)

        old_script_prefix = get_script_prefix()

        try:
            original_path = request.META.get("PATH_INFO", request.path_info)

            # Strip the prefix only if JupyterHub forwarded it as part of the
            # incoming path. Otherwise, it was already stripped upstream.
            if original_path == self.prefix:
                path_info = "/"
            elif original_path.startswith(self.prefix + "/"):
                path_info = original_path[len(self.prefix):]
            else:
                path_info = original_path

            request.META["SCRIPT_NAME"] = self.prefix
            request.META["PATH_INFO"] = path_info
            request.path_info = path_info
            request.path = self.prefix + path_info

            set_script_prefix(self.prefix + "/")
            return self.get_response(request)
        finally:
            set_script_prefix(old_script_prefix)