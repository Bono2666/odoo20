import { patch } from "@web/core/utils/patch";
import { Many2XAutocomplete } from "@web/views/fields/relational_utils";

patch(Many2XAutocomplete.prototype, {
    /**
     * When the connection is lost during a many2one search, Odoo falls back on its offline
     * cache. Over plain http the browser has no WebCrypto, the cache is disabled and the
     * fallback returns `undefined`: the autocomplete then crashes on `records.length`
     * ("Cannot read properties of undefined (reading 'length')").
     * Show no result instead, and do not memoize the failed search so that typing again
     * queries the server once the connection is back.
     */
    async memoizedSearch(name) {
        const records = await super.memoizedSearch(...arguments);
        if (records) {
            return records;
        }
        this.previousSearch = null;
        return [];
    },
});
