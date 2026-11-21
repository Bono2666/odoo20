from odoo.tests import no_retry, tagged

from odoo.addons.web.tests.test_js import HootCommon, unit_test_error_checker


@tagged('post_install', '-at_install')
class TestMinaWebFixJs(HootCommon):

    @no_retry
    def test_unit_desktop(self):
        filters = self._get_hoot_module_filters(['mina_web_fix'], ['mina_web_fix'])
        self.browser_js(
            f'/web/tests?headless&loglevel=2&preset=desktop&timeout=15000{filters}', "", "",
            login='admin', timeout=900,
            success_signal="[HOOT] Test suite succeeded", error_checker=unit_test_error_checker,
        )
