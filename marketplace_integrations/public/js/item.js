// bol offer on an Item (story 05). The offer itself is kept in sync in the background.

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (
			frm.is_new() ||
			!frm.doc.bol_offer_status ||
			frm.doc.bol_offer_status === "Deleted" ||
			!frappe.user.has_role(["System Manager", "Bol Manager"])
		) {
			return;
		}
		frm.add_custom_button(__("Delete bol offer"), () => delete_offer(frm), __("bol"));
	},
});

function delete_offer(frm) {
	frappe.confirm(
		__(
			"Delete the bol offer of {0}? This can't be undone, and Sell on bol is unticked. To take it off sale for now, untick Sell on bol or disable the item instead.",
			[frm.doc.name]
		),
		() => {
			frappe
				.call({
					method: "marketplace_integrations.bol.offers.delete_offer",
					args: { item_code: frm.doc.name },
					freeze: true,
				})
				.then(() => {
					frappe.show_alert({ message: __("bol offer deleted"), indicator: "green" });
					frm.reload_doc();
				});
		}
	);
}
