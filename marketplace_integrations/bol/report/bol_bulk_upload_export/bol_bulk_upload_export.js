// Copyright (c) 2026, Can Demiralp and contributors
// For license information, please see license.txt

// The file for bol's seller dashboard: Items → Add items in bulk. Use Download for bol, not Export: the standard
// export turns the HTML descriptions into flat text.

frappe.query_reports["bol Bulk Upload Export"] = {
	filters: [
		{
			fieldname: "item_group",
			label: __("Item Group"),
			fieldtype: "Link",
			options: "Item Group",
		},
		{ fieldname: "brand", label: __("Brand"), fieldtype: "Link", options: "Brand" },
		{
			fieldname: "template",
			label: __("Template"),
			fieldtype: "Link",
			options: "Item",
			get_query: () => ({ filters: { has_variants: 1 } }),
		},
		{
			fieldname: "only_new",
			label: __("Only items not on bol yet"),
			fieldtype: "Check",
			default: 1,
		},
		{ fieldname: "only_sell_on_bol", label: __("Only Sell on bol items"), fieldtype: "Check" },
	],

	onload(report) {
		report.page.add_inner_button(__("Download for bol"), () =>
			open_url_post("/api/method/marketplace_integrations.bol.bulk_export.download", {
				filters: JSON.stringify(report.get_filter_values()),
			})
		);
	},
};
