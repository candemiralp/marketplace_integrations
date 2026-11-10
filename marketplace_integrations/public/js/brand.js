// bol economic operator on a Brand (story 04). The ID is read-only and set only through this dialog.

frappe.ui.form.on("Brand", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role(["System Manager", "Bol Manager"])) {
			return;
		}
		frm.add_custom_button(__("Select bol economic operator"), () => select_economic_operator(frm));
	},
});

function select_economic_operator(frm) {
	frappe
		.call({
			method: "marketplace_integrations.bol.catalog.list_economic_operators",
			freeze: true,
		})
		.then((r) => {
			const operators = r.message || [];
			if (!operators.length) {
				frappe.msgprint(
					__("bol has no economic operators for this account. Create them in the bol seller dashboard.")
				);
				return;
			}
			const dialog = new frappe.ui.Dialog({
				title: __("Select bol economic operator"),
				fields: [
					{
						fieldname: "operator_id",
						fieldtype: "Select",
						label: __("Economic Operator"),
						reqd: 1,
						default: frm.doc.bol_economic_operator_id,
						options: operators.map((op) => ({ value: op.id, label: `${op.name} (${op.status})` })),
					},
				],
				primary_action_label: __("Save"),
				primary_action: ({ operator_id }) => save_operator(frm, dialog, operator_id),
			});
			if (frm.doc.bol_economic_operator_id) {
				dialog.set_secondary_action_label(__("Clear"));
				dialog.set_secondary_action(() => save_operator(frm, dialog, null));
			}
			dialog.show();
		});
}

function save_operator(frm, dialog, operator_id) {
	frappe
		.call({
			method: "marketplace_integrations.bol.catalog.set_brand_economic_operator",
			args: { brand: frm.doc.name, operator_id },
			freeze: true,
		})
		.then((r) => {
			dialog.hide();
			const status = r.message && r.message.status;
			if (status && status !== "VALID") {
				frappe.msgprint({
					title: __("Economic operator not valid"),
					indicator: "orange",
					message: __(
						"bol reports status {0} for this economic operator. Offers linked to it won't be published until bol marks it VALID.",
						[status]
					),
				});
			}
			frm.reload_doc();
		});
}
