// bol shipment on a Delivery Note (story 08). The shipment itself is confirmed in the background.

frappe.ui.form.on("Delivery Note", {
	onload(frm) {
		// The server sets bol_order_id on save; set it now so the bol.com section shows before the first save.
		const sales_order =
			frm.is_new() && !frm.doc.is_return && (frm.doc.items || [])[0]?.against_sales_order;
		if (!sales_order || frm.doc.bol_order_id) {
			return;
		}
		frappe.db.get_value("Sales Order", sales_order, "bol_order_id").then((r) => {
			const order_id = r.message && r.message.bol_order_id;
			if (order_id) {
				frm.set_value("bol_order_id", order_id);
			}
		});
	},

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
