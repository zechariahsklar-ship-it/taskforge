// Lets the "Create task" page collect checklist items before the task
// exists yet - rows are purely client-side (name="new_checklist_titles")
// and ride along with the rest of the create-task form's normal POST,
// since a checklist item can't be persisted until its task has a pk.
(function () {
    const list = document.querySelector('[data-new-checklist-list]');
    if (!list) {
        return;
    }

    function addRow() {
        const row = document.createElement('div');
        row.className = 'new-checklist-row';
        row.innerHTML =
            '<input type="text" name="new_checklist_titles" class="form-control checklist-title-input" placeholder="Checklist item">' +
            '<button type="button" class="button-link checklist-delete" data-remove-checklist-row>Remove</button>';
        list.appendChild(row);
        row.querySelector('input').focus();
    }

    document.querySelectorAll('[data-add-checklist-row]').forEach(function (button) {
        button.addEventListener('click', addRow);
    });

    list.addEventListener('click', function (event) {
        const removeButton = event.target.closest('[data-remove-checklist-row]');
        if (!removeButton) {
            return;
        }
        if (list.querySelectorAll('.new-checklist-row').length > 1) {
            removeButton.closest('.new-checklist-row').remove();
        } else {
            removeButton.closest('.new-checklist-row').querySelector('input').value = '';
        }
    });
}());
