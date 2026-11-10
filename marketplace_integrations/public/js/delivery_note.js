// bol shipment (story 08) and bol label (story 09) on a Delivery Note. bol's work happens in the background.

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
		if (!frappe.user.has_role(["System Manager", "Bol Manager"])) {
			return;
		}
		add_label_buttons(frm);
		add_retry_button(frm);
	},
});

// Verzenden via bol (story 09): the label is bought on the draft and attached in the background.
function add_label_buttons(frm) {
	if (frm.doc.docstatus !== 0 || frm.is_new()) {
		return;
	}
	const status = frm.doc.bol_label_status;
	if (frm.doc.__onload?.bol_label_allowed && (!status || status === "Failed")) {
		frm.add_custom_button(__("Get bol label"), () => request_label(frm), __("bol"));
	}
	if (status === "Requested" && frm.doc.bol_shipping_label_id) {
		frm.add_custom_button(
			__("Download bol label again"),
			() => download_label(frm),
			__("bol")
		);
	}
}

function request_label(frm) {
	// The server buys the label for the saved items and quantities.
	if (frm.is_dirty()) {
		frappe.msgprint(__("Save the Delivery Note before getting the bol label."));
		return;
	}
	frappe
		.call({
			method: "marketplace_integrations.bol.labels.request_label",
			args: { dn_name: frm.doc.name },
			freeze: true,
		})
		.then(() => {
			frappe.show_alert({
				message: __("bol label requested. It's attached here when bol has made it."),
				indicator: "blue",
			});
			frm.reload_doc();
		});
}

function download_label(frm) {
	frappe
		.call({
			method: "marketplace_integrations.bol.labels.download_label",
			args: { dn_name: frm.doc.name },
			freeze: true,
		})
		.then(() => {
			frappe.show_alert({ message: __("bol label attached"), indicator: "green" });
			frm.reload_doc();
		});
}

function add_retry_button(frm) {
	if (frm.doc.docstatus !== 1 || !["Failed", "Unknown"].includes(frm.doc.bol_shipment_status)) {
		return;
	}
	frm.add_custom_button(__("Retry bol shipment"), () => retry_shipment(frm), __("bol"));
}

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
