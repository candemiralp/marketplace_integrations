// Copyright (c) 2026, Magnatlas and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bol Settings", {
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
	},
});
