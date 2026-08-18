(function () {
    'use strict';

    function cleanText(element) {
        return element ? element.textContent.replace(/\s+/g, ' ').trim() : '';
    }

    function comparisonGridLabel(grid) {
        const bucketPanel = grid.closest('.greeks-bucket-greek-panel');
        if (bucketPanel) {
            const kind = cleanText(bucketPanel.querySelector('.greeks-bucket-greek-kind'));
            const name = cleanText(bucketPanel.querySelector('.greeks-bucket-greek-name'));
            const unit = cleanText(bucketPanel.querySelector('.greeks-bucket-greek-unit'));
            return `${kind}: ${name}${unit ? ` (${unit})` : ''}`;
        }

        const section = grid.closest('.greeks-monitor-section');
        const greek = cleanText(section && section.querySelector('.greeks-monitor-title'))
            .replace(/\s+Ladder$/, '');
        const panel = grid.closest('.greeks-ladder-table-panel, .greeks-unit-ladder-table-panel');
        const subtitle = cleanText(panel && panel.querySelector('.greeks-ladder-subtitle'));
        if (!greek || !subtitle) {
            return '';
        }
        return subtitle === 'By Unit'
            ? `${greek} by maturity and unit`
            : `${greek} by maturity and asset or pair`;
    }

    function applyGridLabels() {
        document.querySelectorAll('.greeks-comparison-grid').forEach((grid) => {
            const treegrid = grid.querySelector('[role="treegrid"]');
            if (!treegrid || treegrid.hasAttribute('aria-label')) {
                return;
            }
            const label = comparisonGridLabel(grid);
            if (label) {
                treegrid.setAttribute('aria-label', label);
            }
        });
    }

    let scheduled = false;
    function scheduleGridLabels() {
        if (scheduled) {
            return;
        }
        scheduled = true;
        window.requestAnimationFrame(() => {
            scheduled = false;
            applyGridLabels();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', scheduleGridLabels, {once: true});
    } else {
        scheduleGridLabels();
    }

    new MutationObserver(scheduleGridLabels).observe(document.documentElement, {
        childList: true,
        subtree: true,
    });
}());
