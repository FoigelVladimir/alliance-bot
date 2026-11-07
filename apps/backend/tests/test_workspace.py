from django.test import SimpleTestCase, override_settings


class WorkspaceTests(SimpleTestCase):
    @override_settings(DEBUG=True)
    def test_home_renders_static_shell(self):
        response = self.client.get("/")
        self.assertContains(response, "Альянс")
        self.assertContains(response, 'type="importmap"')
        self.assertContains(response, "alliance/api")
        self.assertContains(response, "/static/css/app")
