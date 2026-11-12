// Copyright (c) 2026, Can Demiralp and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bol Settings", {
	setup(frm) {
		frm.set_query("price_list", () => ({ filters: { selling: 1 } }));
		frm.set_query("warehouse", () => ({ filters: { is_group: 0, company: frm.doc.company } }));
		for (const field of ["tax_template_nl", "tax_template_be"]) {
			frm.set_query(field, () => ({ filters: { company: frm.doc.company } }));
		}
	},

	refresh(frm) {
		frm.add_custom_button(__("Test Connection"), () => {
			// The server tests the saved credentials, so unsaved edits would be misleading.
			if (frm.is_dirty()) {
				frappe.msgprint(__("Save the settings before testing the connection."));
				return;
			}
			frm.call({ method: "test_connection", doc: frm.doc, freeze: true }).then((r) => {
				if (r.message && r.message.ok) {
					frappe.show_alert({ message: __("Connected to bol"), indicator: "green" });
				}
			});
		});

		if (frm.doc.enabled && !frm.is_dirty()) {
			frm.add_custom_button(__("Adopt existing offers"), () => adopt_existing_offers(frm));
		}
	},
});

function adopt_existing_offers(frm) {
	frappe.confirm(
		__(
			"Map every offer that already exists at bol to its Item by EAN? Nothing is created or changed at bol. You get a message with the result when it's done."
		),
		() => {
			frm.call({ method: "adopt_existing_offers", doc: frm.doc }).then((r) => {
				const queued = r.message && r.message.queued;
				frappe.show_alert({
					message: queued
						? __("Adopting bol offers in the background")
						: __("Adopting bol offers is already running"),
					indicator: queued ? "blue" : "orange",
				});
			});
		}
	);
}
