import re
from datetime import datetime, timedelta

import requests
from sretoolbox.utils import retry

from managedtenants.utils.general_utils import parse_version_from_imageset_name


class OCMAPIError(Exception):
    """Used when there are errors with the OCM API"""

    def __init__(self, message, response):
        super().__init__(message)
        self.response = response


def retry_hook(exception):
    if not isinstance(exception, OCMAPIError):
        return

    if exception.response.status_code < 500:
        raise exception


class OcmCli:
    API = "https://api.stage.openshift.com"
    TOKEN_EXPIRATION_MINUTES = 15

    ADDON_KEYS = {
        "id": "id",
        "name": "name",
        "description": "description",
        "link": "docs_link",
        "icon": "icon",
        "label": "label",
        "enabled": "enabled",
        "installMode": "install_mode",
        "targetNamespace": "target_namespace",
        "ocmQuotaName": "resource_name",
        "ocmQuotaCost": "resource_cost",
        "operatorName": "operator_name",
        "hasExternalResources": "has_external_resources",
        "credentialsRequests": "credentials_requests",
        "addOnParameters": "parameters",
        "addOnRequirements": "requirements",
        "subOperators": "sub_operators",
        "managedService": "managed_service",
        "subscriptionConfig": "config",
    }

    IMAGESET_KEYS = {
        "indexImage": "source_image",
        "addOnParameters": "parameters",
        "addOnRequirements": "requirements",
        "subOperators": "sub_operators",
        "pullSecretName": "pull_secret_name",
        "additionalCatalogSources": "additional_catalog_sources",
        "subscriptionConfig": "config",
    }

    def __init__(self, offline_token, api=API, api_insecure=False):
        self.offline_token = offline_token
        self._token = None
        self._last_token_issue = None
        self.api_insecure = api_insecure

        if api is None:
            self.api = self.API
        else:
            self.api = api

    @property
    @retry(hook=retry_hook, max_attempts=10)
    def token(self):
        now = datetime.utcnow()
        if self._token:
            if now - self._last_token_issue < timedelta(
                minutes=self.TOKEN_EXPIRATION_MINUTES
            ):
                return self._token

        url = (
            "https://sso.redhat.com/auth/realms/"
            "redhat-external/protocol/openid-connect/token"
        )

        data = {
            "grant_type": "refresh_token",
            "client_id": "cloud-services",
            "refresh_token": self.offline_token,
        }

        method = requests.post
        response = method(url, data=data)
        self._raise_for_status(response, reqs_method=method, url=url)
        self._token = response.json()["access_token"]
        self._last_token_issue = now
        return self._token

    def list_addons(self):
        return self._pool_items("/api/clusters_mgmt/v1/addons")

    def list_sku_rules(self):
        return self._pool_items("/api/accounts_mgmt/v1/sku_rules")

    def add_addon(self, metadata):
        addon = self._addon_from_metadata(metadata)
        return self._post("/api/clusters_mgmt/v1/addons", json=addon)

    def _addon_exists(self, addon_id):
        try:
            self._get(f"/api/clusters_mgmt/v1/addons/{addon_id}")
            return True
        # `_get` raises OCMAPIError on 404's
        except OCMAPIError:
            return False

    def add_addon_version(self, imageset, metadata):
        if self._addon_exists(metadata.get("id")):
            return self._add_addon_version(imageset, metadata)
        # Create the addon first
        self.add_addon(metadata)
        return self._add_addon_version(imageset, metadata)

    def _add_addon_version(self, imageset, metadata):
        addon = self._addon_from_imageset(imageset, metadata)
        return self._post(
            f'/api/clusters_mgmt/v1/addons/{metadata.get("id")}/versions',
            json=addon,
        )

    def update_addon(self, metadata):
        addon = self._addon_from_metadata(metadata)
        addon_id = addon.pop("id")
        return self._patch(
            f"/api/clusters_mgmt/v1/addons/{addon_id}", json=addon
        )

    def update_addon_version(self, imageset, metadata):
        addon = self._addon_from_imageset(imageset, metadata)
        version_id = addon.pop("id")
        addon_name = metadata.get("id")
        return self._patch(
            f"/api/clusters_mgmt/v1/addons/{addon_name}/versions/{version_id}",
            json=addon,
        )

    def get_addon(self, addon_id):
        return self._get(f"/api/clusters_mgmt/v1/addons/{addon_id}")

    def delete_addon(self, addon_id):
        return self._delete(f"/api/clusters_mgmt/v1/addons/{addon_id}")

    def enable_addon(self, addon_id):
        return self._patch(
            f"/api/clusters_mgmt/v1/addons/{addon_id}", json={"enabled": True}
        )

    def disable_addon(self, addon_id):
        return self._patch(
            f"/api/clusters_mgmt/v1/addons/{addon_id}", json={"enabled": False}
        )

    def get_addon_migrations(self):
        return self._get("/api/clusters_mgmt/v1/addon_migrations")

    def check_addon_migration_exists(self, addon_id):
        try:
            self._get(f"/api/clusters_mgmt/v1/addon_migrations/{addon_id}")
            return True
        # `_get` raises OCMAPIError on 404's
        except OCMAPIError:
            return False

    def get_addon_migration(self, addon_id):
        return self._get(f"/api/clusters_mgmt/v1/addon_migrations/{addon_id}")

    def post_addon_migration(self, addon_id):
        return self._post(
            "/api/clusters_mgmt/v1/addon_migrations",
            json={
                "addon_id": addon_id,
                "enabled": False,
                "white_listed": False,
                "rollback_migration": False,
            },
        )

    def patch_addon_migration(self, addon_id, patch):
        return self._patch(
            f"/api/clusters_mgmt/v1/addon_migrations/{addon_id}", json=patch
        )

    def delete_addon_migration(self, addon_id):
        return self._delete(
            f"/api/clusters_mgmt/v1/addon_migrations/{addon_id}"
        )

    def disable_addon_installation(self, addon_id):
        try:
            output = self.post_addon_migration(addon_id)
        except OCMAPIError as exception:
            if exception.response.status_code == 409:
                return self.patch_addon_migration(
                    addon_id,
                    {
                        "enabled": False,
                        "white_listed": False,
                        "rollback_migration": False,
                    },
                )
            raise exception
        return output

    def enable_addon_migration(self, addon_id):
        return self.patch_addon_migration(
            addon_id,
            {
                "enabled": True,
            },
        )

    def complete_addon_migration(self, addon_id):
        return self.patch_addon_migration(
            addon_id,
            {
                "enabled": True,
                "white_listed": True,
            },
        )

    def rollback_addon_migration(self, addon_id):
        return self.patch_addon_migration(
            addon_id,
            {
                "enabled": True,
                "white_listed": False,
                "rollback_migration": True,
            },
        )

    def unrollback_addon_migration(self, addon_id):
        return self.patch_addon_migration(
            addon_id,
            {
                "rollback_migration": False,
            },
        )

    def upsert_addon(self, metadata):
        try:
            addon = self.add_addon(metadata)
        except OCMAPIError as exception:
            if exception.response.status_code == 409:
                return self.update_addon(metadata)

            raise exception
        return addon

    # Post Addon version data to versions endpoint
    def upsert_addon_version(self, imageset, metadata):
        try:
            addon = self.add_addon_version(imageset, metadata)
        except OCMAPIError as exception:
            if exception.response.status_code == 409:
                return self.update_addon_version(imageset, metadata)
            raise exception
        return addon

    # Returns a versioned addon payload that corresponds
    # to an ImageSet
    def _addon_from_imageset(self, imageset, metadata):
        addon = {
            "id": str(parse_version_from_imageset_name(imageset.get("name"))),
            "enabled": metadata.get("enabled"),
            "channel": metadata.get("defaultChannel"),
        }

        # Set attributes from metdata file if present.
        # They will get overwritten with the values from the imageset if they're
        # present in the imageset as well.
        if metadata.get("pullSecretName"):
            mapped_key = self.IMAGESET_KEYS["pullSecretName"]
            addon[mapped_key] = metadata.get("pullSecretName")

        if metadata.get("additionalCatalogSources"):
            mapped_key = self.IMAGESET_KEYS["additionalCatalogSources"]
            addon[mapped_key] = self.index_dicts(
                metadata.get("additionalCatalogSources")
            )

        if metadata.get("subscriptionConfig"):
            mapped_key = self.IMAGESET_KEYS["subscriptionConfig"]
            if metadata.get("subscriptionConfig").get("env"):
                addon[mapped_key] = {}
                addon[mapped_key][
                    "add_on_environment_variables"
                ] = self.index_dicts(
                    metadata.get("subscriptionConfig").get("env")
                )

        if metadata.get("addOnParameters"):
            mapped_key = self.IMAGESET_KEYS["addOnParameters"]
            addon[mapped_key] = self._parameters_from_list(
                metadata.get("addOnParameters")
            )

        for key, val in imageset.items():
            if key in self.IMAGESET_KEYS:
                mapped_key = self.IMAGESET_KEYS[key]

                if key == "additionalCatalogSources":
                    catalog_src_list = self.index_dicts(val)
                    addon[mapped_key] = catalog_src_list
                    continue

                if key == "subscriptionConfig":
                    if val.get("env"):
                        addon[mapped_key] = {}
                        env_var_list = self.index_dicts(val["env"])
                        addon[mapped_key][
                            "add_on_environment_variables"
                        ] = env_var_list
                    continue
                if key == "addOnParameters":
                    val = self._parameters_from_list(val)
                addon[mapped_key] = val
        return addon

    def _addon_from_metadata(self, metadata):
        addon = {}
        metadata["addOnParameters"] = metadata.get("addOnParameters", [])
        metadata["addOnRequirements"] = metadata.get("addOnRequirements", [])
        for key, val in metadata.items():
            if key in self.ADDON_KEYS:
                mapped_key = self.ADDON_KEYS[key]

                # Skip adding these parameters as they're present
                # in the ImageSet (versions endpoint)
                if metadata.get("addonImageSetVersion") and key in [
                    "addOnParameters",
                    "addOnRequirements",
                    "subOperators",
                ]:
                    continue
                if key == "installMode":
                    val = _camel_to_snake_case(val)
                if key == "addOnParameters":
                    val = self._parameters_from_list(val)
                if key == "subscriptionConfig":
                    if val.get("env"):
                        addon[mapped_key] = {}
                        environment_variables_list = self.index_dicts(
                            val.get("env")
                        )
                        addon[mapped_key][
                            "add_on_environment_variables"
                        ] = environment_variables_list
                    continue
                addon[mapped_key] = val
        return addon

    @staticmethod
    def _parameters_from_list(params):
        # Enforce a sort order field on addon parameters
        # so that they can be shown in the same order as
        # the metadata file.
        for index, param in enumerate(params):
            param["order"] = index
        return {"items": params}

    @staticmethod
    def index_dicts(dicts):
        return [dict(d, id=str(id)) for id, d in enumerate(dicts)]

    def _headers(self, extra_headers=None):
        headers = {"Authorization": f"Bearer {self.token}"}

        if extra_headers:
            headers.update(extra_headers)

        return headers

    def _url(self, path):
        return f"{self.api}{path}"

    @retry(hook=retry_hook)
    def _api(self, reqs_method, path, **kwargs):
        if self.api_insecure:
            kwargs["verify"] = False
        url = self._url(path)
        headers = self._headers(kwargs.pop("headers", None))
        response = reqs_method(url, headers=headers, **kwargs)
        self._raise_for_status(
            response, reqs_method=reqs_method, url=url, **kwargs
        )
        return response

    def _post(self, path, **kwargs):
        return self._api(requests.post, path, **kwargs)

    def _get(self, path, **kwargs):
        return self._api(requests.get, path, **kwargs)

    def _delete(self, path, **kwargs):
        return self._api(requests.delete, path, **kwargs)

    def _patch(self, path, **kwargs):
        return self._api(requests.patch, path, **kwargs)

    def _pool_items(self, path):
        items = []
        page = 1
        while True:
            result = self._get(path, params={"page": str(page)}).json()

            items.extend(result["items"])
            total = result["total"]
            page += 1

            if len(items) == total:
                break

        return items

    @staticmethod
    def _raise_for_status(response, reqs_method, url, **kwargs):
        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as exception:
            method = reqs_method.__name__.upper()
            error_message = f"Error {method} {url}\n{exception}\n"
            if kwargs.get("params"):
                error_message += f"params: {kwargs['params']}\n"
            if kwargs.get("json"):
                error_message += f"json: {kwargs['json']}\n"
            error_message += f"original error: {response.text}"
            raise OCMAPIError(error_message, response)


def _camel_to_snake_case(val):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", val).lower()
