var dagcomponentfuncs = (window.dashAgGridComponentFunctions =
    window.dashAgGridComponentFunctions || {});

dagcomponentfuncs.PricerLegSelector = function (props) {
    var node = props.node;
    var label = props.value == null ? "" : String(props.value);
    var isPinned = Boolean(node && node.rowPinned);
    var selectedState = React.useState(Boolean(node && node.isSelected()));
    var selected = selectedState[0];
    var setSelected = selectedState[1];
    var checkboxRef = React.useRef(null);

    React.useEffect(
        function () {
            if (!node || isPinned) {
                return undefined;
            }

            var syncSelection = function () {
                setSelected(Boolean(node.isSelected()));
            };
            var toggleWithSpace = function (event) {
                if (
                    event.target !== checkboxRef.current ||
                    (event.code !== "Space" && event.key !== " ")
                ) {
                    return;
                }
                event.preventDefault();
                event.stopImmediatePropagation();
                node.setSelected(!node.isSelected());
            };

            node.addEventListener("rowSelected", syncSelection);
            document.addEventListener("keydown", toggleWithSpace, true);
            syncSelection();

            return function () {
                node.removeEventListener("rowSelected", syncSelection);
                document.removeEventListener("keydown", toggleWithSpace, true);
            };
        },
        [node, isPinned]
    );

    var children = [];
    if (!isPinned) {
        children.push(
            React.createElement("input", {
                key: "selector",
                type: "checkbox",
                className: "pricer-leg-selector-checkbox",
                ref: checkboxRef,
                checked: selected,
                tabIndex: 0,
                "aria-label": (selected ? "Deselect " : "Select ") + label,
                onClick: function (event) {
                    event.stopPropagation();
                },
                onChange: function (event) {
                    node.setSelected(event.target.checked);
                },
            })
        );
    }
    children.push(
        React.createElement(
            "span",
            {key: "label", className: "pricer-leg-selector-label"},
            label
        )
    );

    return React.createElement(
        "span",
        {className: "pricer-leg-selector"},
        children
    );
};
