import { useEffect } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { ListController } from "@web/views/list/list_controller";
import { listView } from "@web/views/list/list_view";

/**
 * Refresh interval, kept in sync with `UPDATE_PRESENCE_DELAY` (60 seconds) so
 * the list stays consistent with what `mail.presence` actually records.
 */
const REFRESH_DELAY = 60_000;

export class AutoRefreshListController extends ListController {
    setup() {
        super.setup();
        useEffect(() => {
            const reload = () => {
                if (document.hidden || this.editedRecord) {
                    return;
                }
                Promise.resolve(this.model.load()).catch(() => {
                    // Ignore transient RPC errors (e.g. lost connection): the
                    // next tick will simply try again.
                });
            };
            const timer = setInterval(reload, REFRESH_DELAY);
            const onVisibilityChange = () => {
                if (!document.hidden) {
                    reload();
                }
            };
            document.addEventListener("visibilitychange", onVisibilityChange);
            return () => {
                clearInterval(timer);
                document.removeEventListener("visibilitychange", onVisibilityChange);
            };
        }, []);
    }
}

registry.category("views").add("mina_online_users.auto_refresh_list", {
    ...listView,
    Controller: AutoRefreshListController,
});
