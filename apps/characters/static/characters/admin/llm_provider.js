(() => {
    "use strict";

    function start() {
        const workspace = document.querySelector("[data-provider-workspace]");
        if (!workspace) return;
        const group = workspace.querySelector("[data-provider-models]");
        const providerSearch = workspace.querySelector("[data-provider-search]");
        providerSearch.addEventListener("input", () => {
            const query = providerSearch.value.trim().toLowerCase();
            workspace.querySelectorAll("[data-provider-source]").forEach(source => {
                source.hidden = !source.textContent.toLowerCase().includes(query);
            });
        });
        if (!group) return;
        group.dataset.enhanced = "true";
        const modelSearch = group.querySelector("[data-model-search]");
        const addButton = group.querySelector("[data-add-model]");
        if (addButton) addButton.hidden = false;

        function cards() {
            return Array.from(group.querySelectorAll("[data-provider-model]:not(.empty-form)"));
        }

        function details(card, open) {
            card.querySelector("[data-model-details]").hidden = !open;
            const button = card.querySelector("[data-model-settings]");
            button.setAttribute("aria-expanded", String(open));
            button.textContent = open ? "收起参数" : "参数";
        }

        function update() {
            const query = modelSearch.value.trim().toLowerCase();
            let count = 0;
            let matches = 0;
            cards().forEach(card => {
                const input = card.querySelector('input[name$="-model"]');
                const aliasInput = card.querySelector('input[name$="-display_name"]');
                const title = card.querySelector("[data-model-title]");
                const alias = card.querySelector("[data-model-alias]");
                if (input) title.textContent = input.value || "新模型";
                if (aliasInput) alias.textContent = aliasInput.value;
                const deleted = card.querySelector('input[name$="-DELETE"]')?.checked || false;
                card.classList.toggle("is-deleted", deleted);
                if (!deleted) count += 1;
                card.hidden = !`${title.textContent} ${alias.textContent}`.toLowerCase().includes(query);
                if (!card.hidden && !deleted) matches += 1;
                const remove = card.querySelector("[data-delete-model]");
                if (remove) remove.textContent = deleted ? "撤销删除" : "删除";
            });
            group.querySelector("[data-model-count]").textContent = String(count);
            const empty = group.querySelector("[data-model-empty]");
            empty.hidden = matches > 0;
            empty.textContent = query ? "没有匹配的模型。" : "此供应商尚未配置模型，点击“添加模型”开始配置。";
        }

        function initializeCards() {
            cards().forEach(card => {
                if (card.dataset.initialized) return;
                card.dataset.initialized = "true";
                details(card, !card.dataset.persisted || Boolean(card.querySelector(".errorlist")));
            });
            update();
        }

        modelSearch.addEventListener("input", update);
        group.addEventListener("input", event => {
            if (event.target !== modelSearch) update();
        });

        group.addEventListener("click", async event => {
            const button = event.target.closest("button");
            if (!button || button.disabled) return;
            if (button.hasAttribute("data-add-model")) {
                modelSearch.value = "";
                group.querySelector(".add-row a")?.click();
                return;
            }
            const card = button.closest("[data-provider-model]");
            if (!card) return;
            if (button.hasAttribute("data-model-settings")) {
                details(card, card.querySelector("[data-model-details]").hidden);
            }
            if (button.hasAttribute("data-delete-model")) {
                const checkbox = card.querySelector('input[name$="-DELETE"]');
                if (checkbox) {
                    checkbox.checked = !checkbox.checked;
                    checkbox.dispatchEvent(new Event("change", {bubbles: true}));
                    update();
                } else {
                    card.querySelector(".inline-deletelink")?.click();
                }
            }
            if (button.hasAttribute("data-test-model")) {
                const result = card.querySelector("[data-model-result]");
                button.disabled = true;
                result.textContent = "正在测试已保存的模型配置…";
                result.classList.remove("is-success", "is-error");
                try {
                    const token = workspace.querySelector('[name="csrfmiddlewaretoken"]').value;
                    const response = await fetch(button.dataset.testModel, {
                        method: "POST",
                        headers: {"X-CSRFToken": token, "Accept": "application/json"},
                        credentials: "same-origin",
                    });
                    const data = await response.json();
                    result.textContent = data.message;
                    result.classList.add(data.ok ? "is-success" : "is-error");
                } catch {
                    result.textContent = "连接测试失败，请检查权限、登录状态或服务连接。";
                    result.classList.add("is-error");
                } finally {
                    button.disabled = false;
                }
            }
        });

        document.addEventListener("formset:added", event => {
            if (event.detail.formsetName !== "models") return;
            initializeCards();
            event.target.querySelector('input[name$="-model"]')?.focus();
        });
        document.addEventListener("formset:removed", update);
        initializeCards();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
