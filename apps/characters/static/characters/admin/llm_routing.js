(() => {
    "use strict";

    const svgNamespace = "http://www.w3.org/2000/svg";
    const paths = {
        up: "M5 12l4-4 4 4",
        down: "M5 8l4 4 4-4",
        remove: "M5 5l8 8M13 5l-8 8",
    };

    function button(action, label) {
        const element = document.createElement("button");
        element.type = "button";
        element.className = "llm-routing-button";
        element.dataset.action = action;
        element.title = label;
        element.setAttribute("aria-label", label);
        const svg = document.createElementNS(svgNamespace, "svg");
        svg.setAttribute("viewBox", "0 0 18 18");
        svg.setAttribute("width", "18");
        svg.setAttribute("height", "18");
        svg.setAttribute("aria-hidden", "true");
        if (action === "drag") {
            element.classList.add("llm-routing-handle");
            for (const x of [6, 12]) {
                for (const y of [4, 9, 14]) {
                    const circle = document.createElementNS(svgNamespace, "circle");
                    circle.setAttribute("cx", String(x));
                    circle.setAttribute("cy", String(y));
                    circle.setAttribute("r", "1.5");
                    circle.setAttribute("fill", "currentColor");
                    svg.append(circle);
                }
            }
        } else {
            const path = document.createElementNS(svgNamespace, "path");
            path.setAttribute("d", paths[action]);
            path.setAttribute("fill", "none");
            path.setAttribute("stroke", "currentColor");
            path.setAttribute("stroke-width", "1.8");
            path.setAttribute("stroke-linecap", "round");
            path.setAttribute("stroke-linejoin", "round");
            svg.append(path);
        }
        element.append(svg);
        return element;
    }

    function initialize(widget) {
        const source = widget.querySelector("[data-routing-source]");
        const editor = widget.querySelector("[data-routing-editor]");
        const picker = widget.querySelector("[data-routing-picker]");
        const list = widget.querySelector("[data-routing-list]");
        const empty = widget.querySelector("[data-routing-empty]");
        const status = widget.querySelector("[data-routing-status]");
        const options = new Map(Array.from(source.options, option => [option.value, option]));
        let order = Array.from(source.selectedOptions, option => option.value);
        let drag = null;

        function sync() {
            for (const option of source.options) {
                option.selected = order.includes(option.value);
            }
            // 浏览器按 option 的 DOM 顺序提交多选值，因此也同步隐藏字段中的顺序。
            for (const id of order) {
                source.append(options.get(id));
            }
            for (const option of picker.options) {
                option.disabled = order.includes(option.value);
            }
            picker.value = "";
            empty.hidden = order.length > 0;
        }

        function render(focusId, focusAction = "drag") {
            list.replaceChildren();
            order.forEach((id, index) => {
                const label = options.get(id).textContent;
                const row = document.createElement("li");
                row.className = "llm-routing-row";
                row.dataset.modelId = id;
                const handle = button("drag", `拖动排序：${label}，也可使用上下方向键`);
                const badge = document.createElement("span");
                badge.className = "llm-routing-badge";
                badge.textContent = `#${index + 1} ${index === 0 ? "首选" : "备用"}`;
                const name = document.createElement("span");
                name.className = "llm-routing-label";
                name.textContent = label;
                const actions = document.createElement("div");
                actions.className = "llm-routing-actions";
                const up = button("up", `上移 ${label}`);
                up.disabled = index === 0;
                const down = button("down", `下移 ${label}`);
                down.disabled = index === order.length - 1;
                actions.append(up, down, button("remove", `移除 ${label}`));
                row.append(handle, badge, name, actions);
                list.append(row);
            });
            sync();
            if (focusId) {
                const row = Array.from(list.children).find(item => item.dataset.modelId === focusId);
                const target = row?.querySelector(`[data-action="${focusAction}"]:not(:disabled)`)
                    || row?.querySelector('[data-action="drag"]');
                (target || picker).focus();
            }
        }

        function changed(message) {
            source.dispatchEvent(new Event("change", {bubbles: true}));
            status.textContent = message;
        }

        function move(id, direction, focusAction) {
            const index = order.indexOf(id);
            const destination = index + direction;
            if (index < 0 || destination < 0 || destination >= order.length) return;
            [order[index], order[destination]] = [order[destination], order[index]];
            render(id, focusAction);
            changed(`${options.get(id).textContent} 已移到第 ${destination + 1} 位`);
        }

        picker.addEventListener("change", () => {
            const id = picker.value;
            if (!id || order.includes(id) || !options.has(id)) return;
            order.push(id);
            render();
            changed(`${options.get(id).textContent} 已加入第 ${order.length} 位`);
        });

        list.addEventListener("click", event => {
            const control = event.target.closest("button[data-action]");
            if (!control || control.disabled) return;
            const id = control.closest("[data-model-id]").dataset.modelId;
            if (control.dataset.action === "up") move(id, -1, "up");
            if (control.dataset.action === "down") move(id, 1, "down");
            if (control.dataset.action === "remove") {
                const index = order.indexOf(id);
                order.splice(index, 1);
                render(order[index] || order[index - 1]);
                if (!order.length) picker.focus();
                changed(`${options.get(id).textContent} 已移出调用链`);
            }
        });

        list.addEventListener("keydown", event => {
            const handle = event.target.closest('[data-action="drag"]');
            if (!handle || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
            event.preventDefault();
            move(handle.closest("[data-model-id]").dataset.modelId, event.key === "ArrowUp" ? -1 : 1, "drag");
        });

        function finishDrag(cancelled = false) {
            if (!drag) return;
            const current = drag;
            drag = null;
            if (current.handle.hasPointerCapture(current.pointerId)) {
                current.handle.releasePointerCapture(current.pointerId);
            }
            if (!cancelled && current.active && current.targetId) {
                order = order.filter(id => id !== current.id);
                const index = order.indexOf(current.targetId) + (current.after ? 1 : 0);
                order.splice(index, 0, current.id);
                render(current.id);
                changed(`${options.get(current.id).textContent} 已移到第 ${index + 1} 位`);
            } else {
                render(current.id);
            }
        }

        list.addEventListener("pointerdown", event => {
            const handle = event.target.closest('[data-action="drag"]');
            if (!handle || event.button !== 0) return;
            event.preventDefault();
            handle.setPointerCapture(event.pointerId);
            drag = {
                id: handle.closest("[data-model-id]").dataset.modelId,
                handle, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
                active: false, targetId: null, after: false,
            };
        });

        list.addEventListener("pointermove", event => {
            if (!drag || event.pointerId !== drag.pointerId) return;
            if (!drag.active && Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY) < 4) return;
            event.preventDefault();
            drag.active = true;
            const sourceRow = drag.handle.closest("[data-model-id]");
            sourceRow.classList.add("is-dragging");
            const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-model-id]");
            for (const row of list.children) {
                row.classList.remove("is-drop-before", "is-drop-after");
            }
            if (!target || !list.contains(target) || target === sourceRow) {
                drag.targetId = null;
                return;
            }
            const bounds = target.getBoundingClientRect();
            drag.targetId = target.dataset.modelId;
            drag.after = event.clientY >= bounds.top + bounds.height / 2;
            target.classList.add(drag.after ? "is-drop-after" : "is-drop-before");
        });

        list.addEventListener("pointerup", () => finishDrag());
        list.addEventListener("pointercancel", () => finishDrag(true));
        window.addEventListener("blur", () => finishDrag(true));
        document.addEventListener("keydown", event => {
            if (drag && event.key === "Escape") {
                event.preventDefault();
                finishDrag(true);
            }
        });

        source.form?.addEventListener("submit", sync);
        render();
        source.hidden = true;
        editor.hidden = false;
    }

    function start() {
        document.querySelectorAll("[data-llm-routing-widget]").forEach(initialize);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
