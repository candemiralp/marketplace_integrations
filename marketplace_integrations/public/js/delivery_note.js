// bol shipment on a Delivery Note (story 08). The shipment itself is confirmed in the background.

frappe.ui.form.on("Delivery Note", {
	refresh(frm) {
		if (
			frm.doc.docstatus !== 1 ||
			!["Failed", "Unknown"].includes(frm.doc.bol_shipment_status) ||
			!frappe.user.has_role(["System Manager", "Bol Manager"])
		) {
			return;
		}
		frm.add_custom_button(__("Retry bol shipment"), () => retry_shipment(frm), __("bol"));
	},
});

function retry_shipment(frm) {
	frappe
		.call({
			method: "marketplace_integrations.bol.shipments.retry_shipment",
			args: { dn_name: frm.doc.name },
			freeze: true,
		})
		.then((r) => {
			const status = r.message && r.message.status;
			// Pending: sent, bol is processing it. The timeline says what happened.
			frappe.show_alert({
				message: __("bol shipment: {0}", [__(status)]),
				indicator: { Confirmed: "green", Pending: "blue" }[status] || "red",
			});
			frm.reload_doc();
		});
}
