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
        const fetchButton = group.querySelector("[data-fetch-models]");
        const catalog = group.querySelector("[data-model-discovery]");
        const catalogSearch = group.querySelector("[data-discovery-search]");
        const catalogList = group.querySelector("[data-discovery-list]");
        const catalogStatus = group.querySelector("[data-discovery-status]");
        const selectVisible = group.querySelector("[data-select-visible]");
        const addSelected = group.querySelector("[data-add-selected]");
        const connectionFields = ["api_type", "base_url", "api_key", "clear_api_key", "timeout", "max_retries"];
        let catalogModels = [];
        const selectedModels = new Set();
        let catalogRequest = null;
        let addingDiscovered = false;
        if (fetchButton) fetchButton.hidden = false;

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
            updateCatalog();
        }

        function initializeCards() {
            cards().forEach(card => {
                if (card.dataset.initialized) return;
                card.dataset.initialized = "true";
                details(card, !card.dataset.persisted || Boolean(card.querySelector(".errorlist")));
            });
            if (!addingDiscovered) update();
        }

        function configuredModels() {
            const result = new Map();
            cards().forEach(card => {
                const input = card.querySelector('input[name$="-model"]');
                const deleted = card.querySelector('input[name$="-DELETE"]')?.checked;
                if (input?.value.trim() && !deleted) result.set(input.value.trim(), card);
            });
            return result;
        }

        function updateCatalog() {
            if (!catalog) return;
            const configured = configuredModels();
            const query = catalogSearch.value.trim().toLowerCase();
            let matches = 0;
            const eligible = [];
            catalogList.querySelectorAll("[data-discovered-model]").forEach(row => {
                const id = row.dataset.discoveredModel;
                const checkbox = row.querySelector("input");
                const added = configured.has(id);
                if (added) selectedModels.delete(id);
                checkbox.disabled = added;
                checkbox.checked = selectedModels.has(id);
                row.querySelector("[data-discovered-state]").textContent = added ? "已在配置列表" : "";
                row.classList.toggle("is-configured", added);
                row.hidden = !row.dataset.searchText.includes(query);
                if (!row.hidden) {
                    matches += 1;
                    if (!added) eligible.push(id);
                }
            });
            group.querySelector("[data-discovery-count]").textContent = String(catalogModels.length);
            group.querySelector("[data-discovery-empty]").hidden = !catalogModels.length || matches > 0;
            const selectedVisible = eligible.filter(id => selectedModels.has(id)).length;
            selectVisible.disabled = !eligible.length || Boolean(catalogRequest);
            selectVisible.checked = eligible.length > 0 && selectedVisible === eligible.length;
            selectVisible.indeterminate = selectedVisible > 0 && selectedVisible < eligible.length;
            addSelected.disabled = !selectedModels.size || Boolean(catalogRequest);
            addSelected.textContent = selectedModels.size ? "添加所选模型（" + selectedModels.size + "）" : "添加所选模型";
            group.querySelector("[data-clear-discovery]").disabled = Boolean(catalogRequest);
        }

        function resetCatalog(message = "") {
            if (!catalog) return;
            catalogRequest?.abort();
            catalogRequest = null;
            catalogModels = [];
            selectedModels.clear();
            catalogList.replaceChildren();
            catalogSearch.value = "";
            catalog.hidden = !message;
            catalogStatus.textContent = message;
            catalogStatus.classList.remove("is-error");
            fetchButton.disabled = false;
            fetchButton.textContent = "获取模型列表";
            updateCatalog();
        }

        function renderCatalog() {
            const rows = document.createDocumentFragment();
            catalogModels.forEach(model => {
                const row = document.createElement("label");
                row.className = "llm-provider-discovered-model";
                row.dataset.discoveredModel = model.id;
                row.dataset.searchText = (model.id + " " + model.display_name).toLowerCase();
                const checkbox = document.createElement("input");
                checkbox.type = "checkbox";
                const identity = document.createElement("span");
                identity.className = "llm-provider-model-identity";
                const title = document.createElement("strong");
                title.textContent = model.id;
                identity.append(title);
                if (model.display_name && model.display_name !== model.id) {
                    const alias = document.createElement("span");
                    alias.className = "llm-provider-model-alias";
                    alias.textContent = model.display_name;
                    identity.append(alias);
                }
                const state = document.createElement("span");
                state.className = "llm-provider-discovered-state";
                state.dataset.discoveredState = "";
                row.append(checkbox, identity, state);
                rows.append(row);
            });
            catalogList.replaceChildren(rows);
            updateCatalog();
        }

        async function fetchCatalog() {
            resetCatalog();
            catalog.hidden = false;
            catalogStatus.textContent = "正在从供应商获取模型列表…";
            const controller = new AbortController();
            catalogRequest = controller;
            fetchButton.disabled = true;
            fetchButton.textContent = "正在获取…";
            updateCatalog();
            const form = group.closest("form");
            const body = new FormData();
            connectionFields.forEach(name => {
                const field = form.elements.namedItem(name);
                if (field && (field.type !== "checkbox" || field.checked)) body.append(name, field.value);
            });
            try {
                const response = await fetch(fetchButton.dataset.fetchModels, {
                    method: "POST", body, signal: controller.signal,
                    headers: {"X-CSRFToken": form.elements.namedItem("csrfmiddlewaretoken").value, "Accept": "application/json"},
                    credentials: "same-origin",
                });
                const data = await response.json();
                if (catalogRequest !== controller) return;
                if (!response.ok || !data.ok) {
                    const errors = Object.values(data.errors || {}).flat().map(error => error.message).join(" ");
                    throw new Error([data.message, errors].filter(Boolean).join(" ") || "获取模型列表失败，请稍后重试。");
                }
                catalogModels = data.models;
                catalogStatus.textContent = catalogModels.length ? "已获取 " + catalogModels.length + " 个模型，请选择要加入配置的模型。" : "供应商未返回可用模型，可以手动添加模型。";
                renderCatalog();
            } catch (error) {
                if (catalogRequest !== controller || error.name === "AbortError") return;
                catalogStatus.textContent = error instanceof SyntaxError ? "获取失败，请检查登录状态、权限或供应商连接。" : error.message;
                catalogStatus.classList.add("is-error");
            } finally {
                if (catalogRequest === controller) {
                    catalogRequest = null;
                    fetchButton.disabled = false;
                    fetchButton.textContent = "获取模型列表";
                    updateCatalog();
                }
            }
        }

        function addDiscoveredModels() {
            const selected = catalogModels.filter(model => selectedModels.has(model.id));
            let added = 0;
            addingDiscovered = true;
            try {
                for (const model of selected) {
                    let card = cards().find(item => item.querySelector('input[name$="-model"]')?.value.trim() === model.id);
                    if (card) {
                        const deleted = card.querySelector('input[name$="-DELETE"]');
                        if (deleted) deleted.checked = false;
                    } else {
                        const total = group.querySelector('input[name$="-TOTAL_FORMS"]');
                        const max = Number(group.querySelector('input[name$="-MAX_NUM_FORMS"]').value);
                        if (max && Number(total.value) >= max) {
                            catalogStatus.textContent = "已达到当前页面可配置的模型数量上限，请先保存配置。";
                            catalogStatus.classList.add("is-error");
                            break;
                        }
                        const index = total.value;
                        const prefix = total.name.slice(0, -"-TOTAL_FORMS".length);
                        group.querySelector(".add-row a")?.click();
                        card = document.getElementById(prefix + "-" + index);
                        if (!card) break;
                        card.querySelector('input[name$="-model"]').value = model.id;
                        const alias = card.querySelector('input[name$="-display_name"]');
                        alias.value = model.display_name.slice(0, alias.maxLength);
                        card.querySelector('input[name$="-is_active"]').checked = true;
                        details(card, false);
                    }
                    selectedModels.delete(model.id);
                    added += 1;
                }
            } finally {
                addingDiscovered = false;
                modelSearch.value = "";
                update();
            }
            if (added && !selectedModels.size) {
                catalogStatus.textContent = "已添加 " + added + " 个模型到配置列表，请点击“保存配置”保存。";
                catalogStatus.classList.remove("is-error");
            }
        }

        if (catalog) {
            catalogSearch.addEventListener("input", updateCatalog);
            catalogList.addEventListener("change", event => {
                const row = event.target.closest("[data-discovered-model]");
                if (!row || event.target.disabled) return;
                if (event.target.checked) selectedModels.add(row.dataset.discoveredModel);
                else selectedModels.delete(row.dataset.discoveredModel);
                updateCatalog();
            });
            selectVisible.addEventListener("change", () => {
                const checked = selectVisible.checked;
                catalogList.querySelectorAll("[data-discovered-model]").forEach(row => {
                    if (row.hidden || row.querySelector("input").disabled) return;
                    if (checked) selectedModels.add(row.dataset.discoveredModel);
                    else selectedModels.delete(row.dataset.discoveredModel);
                });
                updateCatalog();
            });
            group.closest("form").addEventListener("input", event => {
                if (connectionFields.includes(event.target.name) && !catalog.hidden) {
                    resetCatalog("连接配置已变更，请重新获取模型列表。");
                }
            });
        }

        modelSearch.addEventListener("input", update);
        group.addEventListener("input", event => {
            if (event.target.closest("[data-provider-model]")) update();
        });

        group.addEventListener("click", async event => {
            const button = event.target.closest("button");
            if (!button || button.disabled) return;
            if (button.hasAttribute("data-fetch-models")) {
                await fetchCatalog();
                return;
            }
            if (button.hasAttribute("data-add-selected")) {
                addDiscoveredModels();
                return;
            }
            if (button.hasAttribute("data-clear-discovery")) {
                resetCatalog();
                return;
            }
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
            if (!addingDiscovered) event.target.querySelector('input[name$="-model"]')?.focus();
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
