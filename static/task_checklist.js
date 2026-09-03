// Drives the checklist widget shared by the Task Detail and Edit Task
// pages (includes/task_checklist.html): drag-to-reorder and the single-item
// complete checkbox both post lightweight AJAX requests to the task-detail
// URL named in #checklist-config's data attribute, so the widget behaves
// identically no matter which page it's embedded on.
(function () {
    const config = document.getElementById('checklist-config');
    const list = document.querySelector('[data-checklist-list]');
    if (!config || !list) {
        return;
    }

    const csrfToken = getCookie('csrftoken');
    const taskDetailUrl = config.dataset.taskDetailUrl;
    let draggedItem = null;

    function clearIndicators() {
        list.querySelectorAll('.checklist-item').forEach(function (item) {
            item.classList.remove('is-dragging', 'is-drop-before');
        });
        list.classList.remove('has-drop-at-end');
    }

    function getItemAfterPointer(clientY) {
        const items = Array.from(list.querySelectorAll('.checklist-item:not(.is-dragging)'));
        for (const item of items) {
            const rect = item.getBoundingClientRect();
            if (clientY < rect.top + rect.height / 2) {
                return item;
            }
        }
        return null;
    }

    // Use lightweight POST requests so checklist toggles and reordering stay fast.
    async function postChecklistAction(formData) {
        const response = await fetch(taskDetailUrl, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-CSRFToken': csrfToken,
            },
            body: formData.toString(),
        });
        if (!response.ok) {
            throw new Error('Checklist action failed');
        }
        return response;
    }

    // Persist the current DOM order after drag-and-drop finishes.
    async function persistChecklistOrder() {
        const formData = new URLSearchParams();
        formData.set('action', 'checklist_reorder');
        Array.from(list.querySelectorAll('.checklist-item')).forEach(function (item) {
            formData.append('item_ids', item.dataset.checklistId);
        });
        await postChecklistAction(formData);
    }

    list.addEventListener('change', async function (event) {
        const checkbox = event.target.closest('[data-checklist-toggle]');
        if (!checkbox) {
            const rowCheckbox = event.target.closest('.checklist-complete');
            if (rowCheckbox) {
                rowCheckbox.closest('.checklist-item').classList.toggle('is-completed', rowCheckbox.checked);
            }
            return;
        }
        const checklistItem = checkbox.closest('.checklist-item');
        checklistItem.classList.toggle('is-completed', checkbox.checked);
        const formData = new URLSearchParams();
        formData.set('action', 'checklist_toggle');
        formData.set('item_id', checkbox.value);
        formData.set('is_completed', checkbox.checked ? 'true' : 'false');
        try {
            await postChecklistAction(formData);
        } catch (error) {
            window.location.reload();
        }
    });

    list.addEventListener('dragstart', function (event) {
        const grip = event.target.closest('.checklist-grip');
        if (!grip) {
            return;
        }
        const item = grip.closest('.checklist-item');
        if (!item) {
            return;
        }
        draggedItem = item;
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.setData('text/plain', item.dataset.checklistId);
        item.classList.add('is-dragging');
    });

    list.addEventListener('dragend', function () {
        clearIndicators();
        draggedItem = null;
    });

    list.addEventListener('dragover', function (event) {
        if (!draggedItem) {
            return;
        }
        event.preventDefault();
        const beforeItem = getItemAfterPointer(event.clientY);
        clearIndicators();
        draggedItem.classList.add('is-dragging');
        if (beforeItem) {
            beforeItem.classList.add('is-drop-before');
            list.insertBefore(draggedItem, beforeItem);
        } else {
            list.classList.add('has-drop-at-end');
            list.appendChild(draggedItem);
        }
    });

    list.addEventListener('drop', async function (event) {
        if (!draggedItem) {
            return;
        }
        event.preventDefault();
        try {
            await persistChecklistOrder();
            clearIndicators();
        } catch (error) {
            window.location.reload();
        }
    });
}());
