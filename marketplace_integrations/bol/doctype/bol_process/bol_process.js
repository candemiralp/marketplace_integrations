// Copyright (c) 2026, Magnatlas and contributors
// For license information, please see license.txt

frappe.ui.form.on("Bol Process", {
	refresh(frm) {
		if (
			frm.doc.status !== "HANDLER_ERROR" ||
			!frappe.user.has_role(["System Manager", "Bol Manager"])
		) {
			return;
		}
		frm.add_custom_button(__("Retry handler"), () => {
			frm.call({ method: "retry_handler", doc: frm.doc, freeze: true }).then(() => {
				frappe.show_alert({ message: __("Handler completed"), indicator: "green" });
				frm.reload_doc();
			});
		});
	},
});
