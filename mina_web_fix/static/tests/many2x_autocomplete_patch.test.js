import { expect, test } from "@odoo/hoot";
import { queryAllTexts } from "@odoo/hoot-dom";
import { runAllTimers } from "@odoo/hoot-mock";
import {
    contains,
    defineModels,
    fields,
    mockOffline,
    models,
    mountView,
    patchWithCleanup,
} from "@web/../tests/web_test_helpers";
import { OfflinePlugin } from "@web/core/offline/offline_plugin";

class Partner extends models.Model {
    name = fields.Char();
    parent_id = fields.Many2one({ relation: "partner" });

    _records = [
        { id: 1, name: "Mina Abadi Mekanika" },
        { id: 2, name: "Fajar Mas Murni" },
    ];
}

defineModels([Partner]);

test("many2one search does not crash when the connection is lost over plain http", async () => {
    const setOffline = mockOffline();
    // Over plain http there is no WebCrypto: the offline cache returns nothing.
    patchWithCleanup(OfflinePlugin.prototype, {
        async searchMany2XRecords() {
            return undefined;
        },
    });
    await mountView({
        type: "form",
        resModel: "partner",
        resId: 1,
        arch: `<form><field name="parent_id"/></form>`,
    });

    await setOffline(true);
    await contains(".o_field_many2one input").edit("Fajar", { confirm: false });
    await runAllTimers();
    expect(".o-autocomplete.dropdown").toHaveCount(1);
    expect(queryAllTexts(".o-autocomplete.dropdown li")).not.toInclude("Fajar Mas Murni");

    // Back online: typing again queries the server (the failed search is not memoized).
    await setOffline(false);
    await contains(".o_field_many2one input").edit("Fajar M", { confirm: false });
    await runAllTimers();
    expect(queryAllTexts(".o-autocomplete.dropdown li")).toInclude("Fajar Mas Murni");
});
