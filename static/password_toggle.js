// Adds a show/hide eye button to every input marked data-password-toggle
// (set by StyledFormMixin server-side), wrapping it and swapping its type
// between password and text.
(function () {
    function iconMarkup(isVisible) {
        if (isVisible) {
            return '<svg class="password-toggle-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false"><path d="M3 3L21 21" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/><path d="M10.6 5.2C11.05 5.07 11.52 5 12 5C16.5 5 20.12 8.04 21.5 12C21 13.43 20.21 14.7 19.18 15.75" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M14.12 14.12C13.58 14.67 12.83 15 12 15C10.34 15 9 13.66 9 12C9 11.17 9.33 10.42 9.88 9.88" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/><path d="M6.1 6.09C4.12 7.31 2.71 9.35 2.5 12C3.88 15.96 7.5 19 12 19C13.89 19 15.62 18.47 17.08 17.54" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        }
        return '<svg class="password-toggle-icon" viewBox="0 0 24 24" fill="none" aria-hidden="true" focusable="false"><path d="M2.5 12C3.88 8.04 7.5 5 12 5C16.5 5 20.12 8.04 21.5 12C20.12 15.96 16.5 19 12 19C7.5 19 3.88 15.96 2.5 12Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/><circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.8"/></svg>';
    }

    function updateButton(button, input) {
        var isVisible = input.type === "text";
        var label = isVisible ? "Hide password" : "Show password";
        button.innerHTML = iconMarkup(isVisible) + '<span class="visually-hidden">' + label + "</span>";
        button.setAttribute("aria-label", label);
        button.setAttribute("title", label);
        button.setAttribute("aria-pressed", isVisible ? "true" : "false");
    }

    function enhancePasswordInput(input) {
        if (!input || input.dataset.passwordToggleBound === "true") {
            return;
        }
        if (!input.parentNode) {
            return;
        }
        input.dataset.passwordToggleBound = "true";

        var wrapper = document.createElement("span");
        wrapper.className = "password-toggle-field";
        input.parentNode.insertBefore(wrapper, input);
        wrapper.appendChild(input);

        var button = document.createElement("button");
        button.type = "button";
        button.className = "password-toggle-button";
        wrapper.appendChild(button);
        updateButton(button, input);

        button.addEventListener("click", function () {
            input.type = input.type === "password" ? "text" : "password";
            updateButton(button, input);
            input.focus();
            if (typeof input.setSelectionRange === "function") {
                var cursorPosition = input.value.length;
                input.setSelectionRange(cursorPosition, cursorPosition);
            }
        });
    }

    function initPasswordToggles() {
        document.querySelectorAll('[data-password-toggle="true"]').forEach(function (input) {
            enhancePasswordInput(input);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initPasswordToggles);
    } else {
        initPasswordToggles();
    }
})();
