// bol shipment (story 08), bol label (story 09) and bol return (story 11) on a Delivery Note. bol's work happens in the background.

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
		add_return_buttons(frm);
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

// bol returns (story 11): the draft is made by the returns poll; submitting it reports the handling result.
function add_return_buttons(frm) {
	if (!frm.doc.is_return || !frm.doc.bol_rma_id) {
		return;
	}
	if (frm.doc.docstatus === 0 && frm.doc.__onload?.bol_returns_on) {
		frm.add_custom_button(
			__("Report to bol without stock return"),
			() => report_without_return(frm),
			__("bol")
		);
	}
	if (frm.doc.docstatus === 1 && frm.doc.bol_return_status === "Failed") {
		frm.add_custom_button(
			__("Retry bol return report"),
			() => retry_return_report(frm),
			__("bol")
		);
	}
}

function report_without_return(frm) {
	const results = frappe.meta
		.get_docfield("Delivery Note", "bol_handling_result")
		.options.split("\n")
		.filter(Boolean);
	const dialog = new frappe.ui.Dialog({
		title: __("Report to bol without stock return"),
		fields: [
			{
				fieldname: "handling_result",
				fieldtype: "Select",
				label: __("bol Handling Result"),
				options: results,
				default: frm.doc.bol_handling_result || "CUSTOMER_KEEPS_PRODUCT_PAID",
				reqd: 1,
			},
			{
				fieldtype: "HTML",
				options: `<p class="text-muted">${__(
					"bol is told {0} units were handled, and this draft return is deleted: no stock comes back.",
					[frm.doc.bol_expected_qty]
				)}</p>`,
			},
		],
		primary_action_label: __("Report and delete draft"),
		primary_action(values) {
			dialog.hide();
			frappe
				.call({
					method: "marketplace_integrations.bol.returns.report_without_return",
					args: { dn_name: frm.doc.name, handling_result: values.handling_result },
					freeze: true,
				})
				.then((r) => {
					frappe.show_alert({ message: __("Reported to bol"), indicator: "green" });
					frappe.set_route("Form", "Delivery Note", r.message.return_against);
				});
		},
	});
	dialog.show();
}

function retry_return_report(frm) {
	frappe
		.call({
			method: "marketplace_integrations.bol.returns.retry_return_report",
			args: { dn_name: frm.doc.name },
			freeze: true,
		})
		.then((r) => {
			const status = r.message && r.message.status;
			// Open: sent, bol is processing it. The timeline says what happened.
			frappe.show_alert({
				message: __("bol return: {0}", [__(status)]),
				indicator: { Reported: "green", Open: "blue" }[status] || "red",
			});
			frm.reload_doc();
		});
}
