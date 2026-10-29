(function () {
    'use strict';

    let pricerLabelSequence = 0;

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

    function applyPricerLabels() {
        document.querySelectorAll('.pricer-field').forEach((field) => {
            const labelElement = field.querySelector(':scope > .pricer-field-label');
            const fieldLabel = cleanText(labelElement);
            if (!fieldLabel) {
                return;
            }
            if (!labelElement.id) {
                pricerLabelSequence += 1;
                labelElement.id = `pricer-field-label-${pricerLabelSequence}`;
            }
            field.querySelectorAll(
                ':scope > .dash-dropdown-wrapper button.dash-dropdown'
            ).forEach((control) => {
                const selectedValue = control.querySelector('.dash-dropdown-value');
                control.setAttribute('aria-labelledby', [
                    labelElement.id,
                    selectedValue && selectedValue.id,
                ].filter(Boolean).join(' '));
                control.removeAttribute('aria-label');
            });
            field.querySelectorAll(
                ':scope > .dash-datepicker .dash-datepicker-input, '
                + ':scope > .dash-input-container > input.dash-input-element, '
                + ':scope > [role="slider"], '
                + ':scope > .dash-slider-container [role="slider"]'
            ).forEach((control) => {
                control.setAttribute('aria-label', fieldLabel);
            });
        });

        document.querySelectorAll('.pricer-structure-panel').forEach((panel) => {
            const structureTitle = cleanText(
                panel.querySelector(':scope > .pricer-section-header .pricer-section-title')
            );
            panel.querySelectorAll('[role="treegrid"]').forEach((treegrid) => {
                if (!structureTitle) {
                    return;
                }
                const isMonthlyComponents = Boolean(
                    treegrid.closest('.pricer-strip-components-grid')
                );
                treegrid.setAttribute(
                    'aria-label',
                    isMonthlyComponents
                        ? `Monthly strip components for ${structureTitle}`
                        : `Option legs for ${structureTitle}`
                );
            });
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
            applyPricerLabels();
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
