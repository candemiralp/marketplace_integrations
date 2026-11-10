// Cancel on bol (story 10): bol cancels each chosen line; the Sales Order follows once bol has processed it.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (!frappe.user.has_role(["System Manager", "Bol Manager"])) {
			return;
		}
		add_cancel_button(frm);
	},
});

function cancellable_lines(frm) {
	// bol cancels a whole order item, so only lines with nothing delivered.
	return (frm.doc.items || []).filter(
		(row) => row.bol_order_item_id && row.qty > 0 && !row.delivered_qty
	);
}

function add_cancel_button(frm) {
	const reasons = frm.doc.__onload?.bol_cancel_reasons;
	if (frm.doc.docstatus !== 1 || !reasons || !cancellable_lines(frm).length) {
		return;
	}
	frm.add_custom_button(__("Cancel on bol"), () => cancel_dialog(frm, reasons), __("bol"));
}

function cancel_dialog(frm, reasons) {
	const lines = cancellable_lines(frm);
	const requested = lines.some((row) => row.bol_cancellation_requested);
	const fields = lines.map((row) => ({
		fieldname: row.name,
		fieldtype: "Check",
		label: `${row.idx}: ${row.qty} × ${row.item_code}`,
		description: row.bol_cancellation_requested ? __("The buyer asked to cancel this.") : "",
		default: row.bol_cancellation_requested ? 1 : 0,
	}));
	fields.push({
		fieldname: "reason_code",
		fieldtype: "Select",
		label: __("Reason"),
		options: reasons.join("\n"),
		default: requested ? "REQUESTED_BY_CUSTOMER" : reasons[0],
		reqd: 1,
	});

	const dialog = new frappe.ui.Dialog({
		title: __("Cancel on bol"),
		fields: fields,
		primary_action_label: __("Cancel on bol"),
		primary_action(values) {
			const so_details = lines.filter((row) => values[row.name]).map((row) => row.name);
			if (!so_details.length) {
				frappe.msgprint(__("Choose the lines to cancel on bol."));
				return;
			}
			dialog.hide();
			send_cancellation(frm, so_details, values.reason_code);
		},
	});
	dialog.show();
}

function send_cancellation(frm, so_details, reason_code) {
	frappe
		.call({
			method: "marketplace_integrations.bol.cancellations.cancel_bol_items",
			args: { sales_order: frm.doc.name, so_details: so_details, reason_code: reason_code },
			freeze: true,
		})
		.then((r) => {
			const result = r.message || {};
			if (result.failed && result.failed.length) {
				let message = result.failed
					.map((failure) => frappe.utils.escape_html(failure.message))
					.join("<br>");
				if (result.resynced) {
					message += "<br>" + __("The Sales Order was brought in line with bol.");
				}
				frappe.msgprint({ title: __("Not all lines cancelled on bol"), message: message });
			}
			if (result.sent && result.sent.length) {
				frappe.show_alert({
					message: __("Sent to bol. The Sales Order is adjusted when bol has cancelled the lines."),
					indicator: "blue",
				});
			}
			frm.reload_doc();
		});
}
