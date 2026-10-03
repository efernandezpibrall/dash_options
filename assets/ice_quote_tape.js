var dagcomponentfuncs = window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {};
dagcomponentfuncs.IceTapeEdge = function (props) {
    const data = props.data || {};
    return React.createElement('span', {className: 'ice-tape-edge ice-tape-edge-' + (data.edge_marker || 'none'), title: data.edge_tooltip},
        React.createElement('span', {className: 'ice-tape-marker ice-tape-marker-' + (data.edge_marker || 'none'), 'aria-hidden': true}),
        React.createElement('span', null, data.edge_display || '—'));
};

var dagfuncs = window.dashAgGridFunctions = window.dashAgGridFunctions || {};
dagfuncs.IceTapeResponsive = function (params) {
    const pinned = params.clientWidth >= 650 ? 'left' : null;
    const columns = ['observed_at', 'contract_label', 'strategy_display'];
    const current = params.api.getColumnState();
    if (columns.some(id => current.find(col => col.colId === id)?.pinned !== pinned)) {
        params.api.applyColumnState({state: columns.map(colId => ({colId: colId, pinned: pinned}))});
    }
};

// Delivery is deliberately neutral so its status cannot be mistaken for a trade side.
dagcomponentfuncs.IceTapeDelivery = function (props) {
    const label = props.value || '—';
    const state = label === 'Sent' ? 'sent' : (label === 'Failed' ? 'failed' : 'neutral');
    return React.createElement('span', {className: 'ice-tape-delivery ice-tape-delivery-' + state}, label);
};
