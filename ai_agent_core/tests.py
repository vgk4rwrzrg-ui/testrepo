"""Test suite for ai_agent_core (run: manage.py test ai_agent_core)."""
from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase, override_settings

from .middleware import JupyterHubProxyMiddleware
from .models import TableAccessAudit, TableAccessPolicy
from .utils import (guarded_aggregate_data, guarded_describe_model,
                    guarded_fetch_data, guarded_list_models)

FIELDS = ["id", "username", "email", "is_active"]


class AccessControlTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.hr = Group.objects.create(name="HR")
        cls.alice = User.objects.create_user("alice")
        cls.alice.groups.add(cls.hr)
        cls.bob = User.objects.create_user("bob")
        cls.root = User.objects.create_superuser("root", "r@x.com", "pw")
        cls.policy = TableAccessPolicy.objects.create(
            app_label="auth", model_name="User",
            access_level="read", allowed_fields=FIELDS, priority=10)
        cls.policy.groups.add(cls.hr)

    def test_group_member_can_fetch_with_narrowed_fields(self):
        r = guarded_fetch_data(self.alice, "auth.User")
        self.assertTrue(r["ok"])
        self.assertEqual(set(r["data"]["rows"][0]), set(FIELDS))

    def test_non_member_is_denied_and_audited(self):
        r = guarded_fetch_data(self.bob, "auth.User")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "PERMISSION_DENIED")
        self.assertTrue(TableAccessAudit.objects.filter(
            username="bob", was_allowed=False).exists())

    def test_filter_on_policy_hidden_field_is_blocked(self):
        r = guarded_fetch_data(self.alice, "auth.User",
                               filters={"last_login__isnull": True})
        self.assertFalse(r["ok"])
        self.assertIn("hidden", r["error"]["message"])

    def test_superuser_bypass(self):
        r = guarded_fetch_data(self.root, "auth.User", limit=2)
        self.assertTrue(r["ok"])

    def test_aggregate_and_discovery(self):
        self.assertEqual(guarded_aggregate_data(
            self.alice, "auth.User", "count", "id")["data"], 3)
        self.assertEqual(list(guarded_list_models(self.alice)["data"]),
                         ["auth.User"])
        desc = guarded_describe_model(self.alice, "auth.User")
        self.assertTrue(all(f["name"] in FIELDS
                            for f in desc["data"]["fields"]))

    def test_high_priority_deny_overrides_allow(self):
        TableAccessPolicy.objects.create(
            app_label="auth", model_name="User", access_level="deny",
            priority=99, applies_to_all_authenticated=True)
        self.assertFalse(guarded_fetch_data(self.alice, "auth.User")["ok"])

    def test_aggregate_only_blocks_row_fetch(self):
        self.policy.access_level = "aggregate_only"
        self.policy.save()
        self.assertFalse(guarded_fetch_data(self.alice, "auth.User")["ok"])
        self.assertTrue(guarded_aggregate_data(
            self.alice, "auth.User", "count", "id")["ok"])

    def test_policy_clean_rejects_non_allowlisted_table(self):
        with self.assertRaises(ValidationError):
            TableAccessPolicy(app_label="auth",
                              model_name="Nope").full_clean(exclude=["groups"])

    def test_policy_clean_rejects_non_allowlisted_field(self):
        with self.assertRaises(ValidationError):
            TableAccessPolicy(app_label="auth", model_name="User",
                              allowed_fields=["password"]
                              ).full_clean(exclude=["groups"])


class MiddlewareTests(TestCase):
    def _mw(self):
        return JupyterHubProxyMiddleware(lambda req: req)

    def test_prefix_header_is_stripped_into_script_name(self):
        req = RequestFactory().get(
            "/user/alice/aiagent/healthz/",
            HTTP_X_FORWARDED_PREFIX="/user/alice")
        req = self._mw()(req)
        self.assertEqual(req.META["SCRIPT_NAME"], "/user/alice")
        self.assertEqual(req.path_info, "/aiagent/healthz/")
        self.assertEqual(req.path, "/user/alice/aiagent/healthz/")

    def test_path_inference_without_header(self):
        req = RequestFactory().get("/user/bob/lab/")
        req = self._mw()(req)
        self.assertEqual(req.META["SCRIPT_NAME"], "/user/bob")

    @override_settings(AI_AGENT_TRUSTED_PROXY_COUNT=2)
    def test_client_ip_uses_trusted_hop_count(self):
        req = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="203.0.113.7, 10.0.0.5")
        req = self._mw()(req)
        self.assertEqual(req.META["AI_AGENT_CLIENT_IP"], "203.0.113.7")

    @override_settings(AI_AGENT_TRUSTED_PROXY_COUNT=2)
    def test_spoofed_extra_hops_do_not_fool_resolution(self):
        # A malicious client pre-set XFF; proxies appended the real chain.
        req = RequestFactory().get(
            "/", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.7, 10.0.0.5")
        req = self._mw()(req)
        self.assertEqual(req.META["AI_AGENT_CLIENT_IP"], "203.0.113.7")
