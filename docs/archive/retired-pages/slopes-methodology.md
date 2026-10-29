# Slopes Page Methodology

## Overview
The Slopes page is a Dash-based analytical tool designed to calculate and visualize the JKM/Brent slope (price ratio) over time. This methodology document explains in detail how the slopes are constructed, calculated, and displayed.

---

## 1. Data Sources

### 1.1 Database Connection
- **Database**: Trino (distributed SQL query engine)
- **Catalogs**:
  - `raw.ice_gas.cleared_gas` - ICE LNG futures data
  - `raw.ice_oil.cleared_oil` - ICE Oil futures data
- **Authentication**: JWT token-based authentication

### 1.2 Data Queries

#### Gas Data (JKM)
```sql
SELECT *
FROM raw."ice_gas"."cleared_gas"
WHERE (product = 'LNG Futures' AND contract = 'JKM')
  AND ("contract_type" != 'P' AND "contract_type" != 'C')
  AND product NOT LIKE '%daily%'
```

**Key Points**:
- Filters for JKM (Japan Korea Marker) LNG futures
- Excludes Put ('P') and Call ('C') options (only futures)
- Excludes daily products

#### Oil Data (Brent)
```sql
SELECT *
FROM raw."ice_oil"."cleared_oil"
WHERE (product = 'Brent Crude Futures' AND contract = 'B')
```

**Key Points**:
- Filters for Brent crude futures
- Contract identifier 'B' represents Brent

### 1.3 Key Data Fields
- **`trade_date`**: Date when the trade occurred (observation date)
- **`strip`**: Contract delivery period (e.g., 'Jan-25', 'Feb-25')
- **`maturity_date`**: Parsed datetime from strip field (contract delivery date)
- **`expiration_date`**: Date when the contract expires
- **`settlement_price`**: Daily settlement price for the contract
- **`product`**: Product identifier (JKM, Brent)
- **`contract`**: Contract code

---

## 2. Data Processing Pipeline

### 2.1 Data Loading and Combination
1. **Load Data**: Retrieve JKM and Brent data from separate connections
2. **Product Labeling**:
   - Rename JKM product to 'JKM'
   - Rename Brent contract and product to 'Brent'
3. **Concatenate**: Combine both datasets into single DataFrame `df_options`

### 2.2 Maturity Date Parsing
The `maturity_date` is critical for grouping contracts by delivery period:

```python
if 'strip' in data.columns:
    # Strip field format is typically 'MMM-YY' like 'Jan-25'
    data['maturity_date'] = pd.to_datetime(data['strip'], format='%b-%y', errors='coerce')
```

**Example**:
- `strip = 'Jan-25'` → `maturity_date = 2025-01-01`
- `strip = 'Dec-24'` → `maturity_date = 2024-12-01`

### 2.3 Brent Lagged Indices Generation

**Background**: In the LNG industry, contracts are often priced based on **lagged** Brent indices rather than spot Brent prices. This reflects the time lag between oil price changes and LNG contract pricing.

#### 2.3.1 Common Brent Indices

The system generates four standard Brent indices used in LNG contract pricing:

| Index | Notation | Lag | Averaging Window | Description |
|-------|----------|-----|------------------|-------------|
| **Brent 301** | B31 | 3 months | 1 month | Month M-3 |
| **Brent 303** | B33 | 3 months | 3 months | Months M-5 to M-3 |
| **Brent 601** | B61 | 6 months | 1 month | Month M-6 |
| **Brent 603** | B63 | 6 months | 3 months | Months M-8 to M-6 |

**Index Notation**: `B{lag_months}{window_months}`

#### 2.3.2 Calculation Methodology

For each Brent index, the system calculates a synthetic price based on historical spot Brent data:

```python
def calculate_lagged_brent_index(df_brent, trade_date, maturity_date,
                                  lag_months, window_months):
    """
    Calculate lagged Brent index

    Example: Brent 303 for January 2025 delivery on trade date 2024-10-15
    - maturity_date = 2025-01-01 (January 2025)
    - lag_months = 3
    - window_months = 3
    - reference_month = 2025-01-01 - 3 months = 2024-10-01
    - window_start = 2024-10-01 - (3-1) months = 2024-08-01
    - window_end = 2024-10-31
    - Average all spot Brent prices from Aug-Oct 2024
    """
```

**Key Steps**:

1. **Calculate Reference Month**:
   ```
   reference_month = maturity_date - lag_months
   ```

2. **Calculate Averaging Window**:
   ```
   window_start = reference_month - (window_months - 1)
   window_end = reference_month (last day of month)
   ```

3. **Filter Spot Brent Data**:
   - Must be on or before trade_date (no forward-looking)
   - Must fall within averaging window
   - Must be original spot Brent (contract = 'B')

4. **Calculate Average**:
   ```python
   lagged_price = spot_brent_prices.mean()
   ```

5. **Create Synthetic Row**:
   - Same trade_date and strip as original
   - Contract code = f'B{lag_months}{window_months}'
   - Settlement price = lagged_price

#### 2.3.3 Calculation Examples

**Example 1: Brent 303 for January 2025 delivery**

**Scenario**:
- Trade Date: 2024-10-15
- Delivery Month: January 2025 (maturity_date = 2025-01-01)
- Index: Brent 303 (lag=3, window=3)

**Calculation**:
1. Reference month: Jan 2025 - 3 months = **October 2024**
2. Window start: Oct 2024 - 2 months = **August 2024**
3. Window end: **October 2024**
4. Averaging period: **August 1, 2024 to October 31, 2024**
5. Average all spot Brent prices in this period
6. Result: B33 price for Jan-25 delivery = $77.85/bbl (example)

**Example 2: Brent 601 for March 2025 delivery**

**Scenario**:
- Trade Date: 2024-10-15
- Delivery Month: March 2025 (maturity_date = 2025-03-01)
- Index: Brent 601 (lag=6, window=1)

**Calculation**:
1. Reference month: Mar 2025 - 6 months = **September 2024**
2. Window: Only September 2024 (1-month window)
3. Averaging period: **September 1-30, 2024**
4. Average all spot Brent prices in September 2024
5. Result: B61 price for Mar-25 delivery = $79.20/bbl (example)

#### 2.3.4 Data Structure After Generation

After generating lagged indices, the data structure expands:

```
Original Brent Data:
trade_date  | product | contract | strip   | maturity_date | settlement_price
2024-10-15  | Brent   | Brent    | Jan-25  | 2025-01-01    | 80.50 (spot)

Synthetic Brent Indices (added):
2024-10-15  | Brent   | B31      | Jan-25  | 2025-01-01    | 78.20 (M-3, 1M)
2024-10-15  | Brent   | B33      | Jan-25  | 2025-01-01    | 77.85 (M-3, 3M)
2024-10-15  | Brent   | B61      | Jan-25  | 2025-01-01    | 76.40 (M-6, 1M)
2024-10-15  | Brent   | B63      | Jan-25  | 2025-01-01    | 75.95 (M-6, 3M)
```

#### 2.3.5 Why Lagged Indices Matter for LNG

**Industry Practice**:
- Most long-term LNG contracts use lagged Brent pricing, not spot
- Common formulas: `LNG Price = α × Brent_303 + β`
- Lag provides price stability and predictability
- Allows buyers/sellers to hedge based on known historical prices

**Typical Lag Structures by Region**:
- **Asian LNG**: Predominantly Brent 303 (3-month lag, 3-month average)
- **European LNG**: Mix of Brent 303 and Brent 601
- **Spot LNG**: May use Brent 301 or spot Brent

**Impact on Slope Analysis**:
- Lagged slopes show the **realized** contract pricing relationship
- Spot slopes show **current market** dynamics
- Comparing spot vs lagged slopes reveals pricing time lags

#### 2.3.6 Custom Index Generation

Users can create custom Brent indices through the UI:

**Options**:
- **Lag**: 0-12 months
- **Window**: 1-12 months
- **Examples**:
  - B00: Spot Brent (no lag, no averaging)
  - B12: 1-month lag, 2-month average
  - B123: 12-month lag, 3-month average

**Validation**:
- Requires sufficient historical data (lag + window months before earliest trade date)
- Incomplete data windows return None (excluded from results)

#### 2.3.7 Data Validation

After generation, the system validates:

```python
validation = {
    'spot_exists': True/False,
    'spot_count': number of spot Brent rows,
    'completeness': {
        'B31': 95.2%,  # Percentage of spot rows successfully calculated
        'B33': 94.8%,
        'B61': 89.3%,  # Lower due to longer lookback requirement
        'B63': 88.1%
    }
}
```

**Completeness Factors**:
- Lower for longer lags (need more historical data)
- Lower for recent delivery months (averaging window may be incomplete)
- 100% completeness rare due to data gaps and edge cases

---

## 3. Time Period Grouping

The slopes can be analyzed across different time aggregations. The `slope_group_data_by_period()` function handles this.

### 3.1 Monthly Grouping
**Logic**: Each contract is represented individually by its delivery month.

```python
data['period'] = data['maturity_date'].dt.strftime('%b-%y')
```

**Example**:
- Contracts: Jan-25, Feb-25, Mar-25, Apr-25...
- No averaging - each month shown separately

### 3.2 Quarterly Grouping
**Logic**: Contracts are grouped by calendar quarter (Q1, Q2, Q3, Q4).

```python
data['quarter'] = data['maturity_date'].dt.quarter
data['year'] = data['maturity_date'].dt.year
data['period'] = f"{year}-Q{quarter}"
```

**Aggregation**:
- Groups: `['product', 'trade_date', 'period']`
- Metric: Average settlement price within each quarter

**Example**:
- Q1 2025: Average of Jan-25, Feb-25, Mar-25
- Q2 2025: Average of Apr-25, May-25, Jun-25

### 3.3 Season Grouping
**Logic**: Contracts are grouped by thermal seasons.

**Season Definition**:
- **Summer**: May through September (months 5-9)
- **Winter**: October through April (months 10-12, 1-4)

```python
def get_season(date):
    month = date.month
    year = date.year
    if 5 <= month <= 9:
        return f"{year}-Summer"
    else:
        return f"{year}-Winter"
```

**Aggregation**:
- Groups: `['product', 'trade_date', 'period']`
- Metric: Average settlement price within each season

**Example**:
- 2025-Summer: Average of May-25, Jun-25, Jul-25, Aug-25, Sep-25
- 2025-Winter: Average of Oct-24, Nov-24, Dec-24, Jan-25, Feb-25, Mar-25, Apr-25

### 3.4 Calendar Grouping
**Logic**: Contracts are grouped by calendar year based on their delivery year.

```python
data['period'] = data['maturity_date'].dt.year.astype(str)
```

**Aggregation**:
- Groups: `['product', 'trade_date', 'period']`
- Metric: Average settlement price across all 12 months of the year

**Example**:
- 2024: Average of all contracts from Jan-24 through Dec-24
- 2025: Average of all contracts from Jan-25 through Dec-25

**Important Notes**:
- Uses `maturity_date` (contract delivery date), NOT `trade_date` (observation date)
- Incomplete years (e.g., 2025 with data only through October) will show averages of available contracts
- This represents the "strip average" for that calendar year

---

## 4. Slope Calculation

### 4.1 Overview
The slope represents the ratio of JKM to Brent prices, expressed as a percentage:

```
Slope (%) = (JKM Price / Brent Price) × 100
```

### 4.2 JKM Discount Adjustment
Users can apply a discount to JKM prices before calculating the slope:

```python
if jkm_discount != 0:
    jkm_data['settlement_price'] = jkm_data['settlement_price'] - jkm_discount
```

**Use Case**: Adjusting for transport costs, regional premiums, or basis differentials.

### 4.3 Matching Logic
The slope calculation requires matching JKM and Brent contracts:

1. **Filter by Product**: Separate JKM and Brent data
2. **Merge on Keys**: `['trade_date', 'strip']`
   - **`trade_date`**: Ensures we're comparing prices from the same observation date
   - **`strip`**: Ensures we're comparing contracts with the same delivery period

**Example Match**:
```
JKM:   trade_date=2024-05-15, strip='Jan-25', price=12.50
Brent: trade_date=2024-05-15, strip='Jan-25', price=75.00
→ Slope = (12.50 / 75.00) × 100 = 16.67%
```

### 4.4 Calculation Steps

```python
# Step 1: Merge JKM and Brent on matching keys
merged = pd.merge(
    jkm_data[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'jkm_price'}),
    brent_data[['trade_date', 'strip', 'settlement_price']].rename(columns={'settlement_price': 'brent_price'}),
    on=['trade_date', 'strip'],
    how='inner'
)

# Step 2: Calculate slope percentage
merged['slope_percentage'] = ((merged['jkm_price'] / merged['brent_price'])) * 100
merged['slope_percentage'] = merged['slope_percentage'].round(2)
```

### 4.5 Output Fields
- **`trade_date`**: Observation date
- **`strip`**: Period identifier (e.g., '2025' for calendar, 'Jan-25' for monthly)
- **`slope_percentage`**: Calculated slope (%)
- **`slope_name`**: 'JKM/Brent'
- **`jkm_price`**: JKM settlement price (potentially discounted)
- **`brent_price`**: Brent settlement price

---

## 5. Visualization

### 5.1 Time Series Charts
**Purpose**: Show how slopes evolve over time for different delivery periods.

**Chart Components**:
- **X-axis**: `trade_date` (observation date)
- **Y-axis**: `slope_percentage` (%)
- **Series**: One line per `strip` (delivery period)

**Grouping Impact**:
- **Monthly**: Many lines (one per month)
- **Quarterly**: Fewer lines (one per quarter)
- **Season**: Two lines per year (Summer, Winter)
- **Calendar**: One line per year

**Features**:
- Hover mode: 'x unified' (shows all values at a given date)
- Line width: 2px
- Markers: Enabled
- Legend: Shows strip identifiers

### 5.2 Data Tables
**Purpose**: Show latest slope values and compare to historical reference dates.

#### Latest Slope Table
Shows the most recent slope value for each strip:

**Columns**:
- **Strip**: Period identifier
- **Latest Date**: Most recent trade date with data
- **Latest Slope (%)**: Current slope value
- **JKM Price**: Current JKM price (with discount indicator if applicable)
- **Brent Price**: Current Brent price

#### Comparison Table (if comparison date selected)
**Additional Columns**:
- **Comparison Date**: Historical reference date
- **Comparison Slope (%)**: Historical slope value
- **Change (%)**: Difference between latest and comparison slope

**Logic**:
```python
# Find latest data
latest_data = slope_data.loc[slope_data.groupby('strip')['trade_date'].idxmax()]

# Find closest comparison data
for strip in slope_data['strip'].unique():
    strip_data = slope_data[slope_data['strip'] == strip]
    closest_idx = (strip_data['trade_date'] - comparison_date).abs().idxmin()
    comparison_data.append(strip_data.loc[closest_idx])

# Calculate change
change = latest_slope - comparison_slope
```

**Conditional Formatting**:
- Green text: Positive change (slope increased)
- Red text: Negative change (slope decreased)
- Bold: Change values

---

## 6. User Controls

### 6.1 Slopes Dropdown
- **Options**: Currently only 'JKM/Brent'
- **Multi-select**: Enabled (for future expansion)
- **Purpose**: Select which slope ratios to display

### 6.2 JKM Discount Input
- **Type**: Numeric input
- **Default**: 0
- **Units**: $/MMBtu
- **Purpose**: Adjust JKM price before slope calculation

### 6.3 Brent Index Selector
- **Type**: Dropdown with preset options + custom
- **Default**: 303 (M-3, 3M avg) - Most common in Asian LNG contracts
- **Options**:
  - **Spot (B)**: Spot Brent (no lag, no averaging)
  - **301 (M-3, 1M avg)**: 3-month lag, 1-month average
  - **303 (M-3, 3M avg)**: 3-month lag, 3-month average (Default)
  - **601 (M-6, 1M avg)**: 6-month lag, 1-month average
  - **603 (M-6, 3M avg)**: 6-month lag, 3-month average
  - **Custom...**: User-defined lag (0-12 months) and window (1-12 months)
- **Purpose**: Select which Brent pricing methodology to use for slope calculation
- **Impact**:
  - Affects both charts and tables
  - Chart title shows selected index (e.g., "JKM/Brent 303 (M-3, 3M) Slope")
  - Different indices will show different slope values due to lag effects

**Custom Index Creation**:
When "Custom..." is selected, two additional inputs appear:
- **Lag (months)**: 0-12 months before delivery
- **Window (months)**: 1-12 months to average

Example: Lag=12, Window=6 creates "Brent 1206" (12-month lag, 6-month average)

### 6.4 Compare to Date Picker
- **Default**: Previous business date
- **Purpose**: Historical reference point for comparison
- **Impact**: Only affects table (adds comparison columns)

### 6.5 Group By Dropdown
- **Options**: Monthly, Quarterly, Season, Calendar
- **Default**: Calendar
- **Clearable**: No (always requires a selection)
- **Impact**: Determines aggregation level for all visualizations

---

## 7. Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ 1. DATA ACQUISITION                                         │
├─────────────────────────────────────────────────────────────┤
│ Trino DB → QUERY_GAS → df_options_gas (JKM)                │
│ Trino DB → QUERY_OIL → df_options_oil (Brent spot)         │
│ Concatenate → df_options (combined)                         │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 2. GENERATE BRENT LAGGED INDICES                            │
├─────────────────────────────────────────────────────────────┤
│ For each (trade_date, strip) in Brent spot data:           │
│   - Calculate B31 (M-3, 1M avg)                             │
│   - Calculate B33 (M-3, 3M avg)                             │
│   - Calculate B61 (M-6, 1M avg)                             │
│   - Calculate B63 (M-6, 3M avg)                             │
│ Create synthetic rows with lagged prices                    │
│ Append to df_options                                        │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 3. PARSE MATURITY DATE                                      │
├─────────────────────────────────────────────────────────────┤
│ strip → maturity_date (datetime)                            │
│ Example: 'Jan-25' → 2025-01-01                              │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 4. USER SELECTS BRENT INDEX                                 │
├─────────────────────────────────────────────────────────────┤
│ User chooses: Spot, B31, B33, B61, B63, or Custom           │
│ Filter Brent data by selected contract code                 │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 5. GROUP BY PERIOD                                          │
├─────────────────────────────────────────────────────────────┤
│ Monthly:    maturity_date → 'Jan-25', 'Feb-25', ...        │
│ Quarterly:  maturity_date → '2025-Q1', '2025-Q2', ...      │
│ Season:     maturity_date → '2025-Summer', '2025-Winter'   │
│ Calendar:   maturity_date → '2024', '2025', ...             │
│                                                             │
│ Aggregate: AVG(settlement_price) by [product, trade_date,  │
│            period]                                          │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 6. APPLY JKM DISCOUNT                                       │
├─────────────────────────────────────────────────────────────┤
│ JKM settlement_price = settlement_price - discount          │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 7. MERGE JKM AND SELECTED BRENT INDEX                       │
├─────────────────────────────────────────────────────────────┤
│ Join on: [trade_date, strip]                                │
│ Inner join (only matching pairs)                            │
│ Uses selected Brent index (Spot, B31, B33, B61, B63)        │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 8. CALCULATE SLOPE                                          │
├─────────────────────────────────────────────────────────────┤
│ slope_percentage = (jkm_price / brent_index_price) × 100   │
└─────────────────┬───────────────────────────────────────────┘
                  │
                  ▼
┌─────────────────────────────────────────────────────────────┐
│ 9. VISUALIZATION                                            │
├─────────────────────────────────────────────────────────────┤
│ Charts: Time series by strip with zoom to last 180 days    │
│ Tables: Latest values + comparison                          │
│ Title includes selected Brent index                         │
└─────────────────────────────────────────────────────────────┘
```

---

## 8. Key Design Decisions

### 8.1 Why Use Maturity Date for Grouping?
**Rationale**: The maturity date represents the actual delivery period of the commodity. When analyzing calendar year averages, we want to know the average price for commodities delivered in that year, not when the contracts were traded.

**Example**:
- A trader on 2024-03-15 can trade contracts for delivery in Jan-25, Feb-25, etc.
- When grouping by calendar, we want Jan-25 to be part of "2025", not "2024"

### 8.2 Why Inner Join on Merge?
**Rationale**: The slope is only meaningful when both JKM and Brent have matching data for the same delivery period on the same observation date.

**Impact**:
- Eliminates periods where only one product has data
- Ensures apples-to-apples comparison
- May reduce data points if one product has gaps

### 8.3 Why Percentage Slope?
**Rationale**:
- Easier to interpret than raw ratios
- Industry standard for comparing energy spreads
- Allows quick mental calculation of equivalent prices

**Example**:
- Slope of 20% means JKM is 20% of Brent price
- If Brent = $80/bbl, JKM ≈ $16/MMBtu

### 8.4 Why Allow JKM Discount?
**Rationale**:
- Accounts for shipping costs from liquefaction to destination
- Regional basis adjustments
- Contract-specific premiums/discounts
- Scenario analysis

---

## 9. Calculation Examples

### Example 1: Monthly Grouping
**Input Data**:
```
trade_date    strip    product  settlement_price
2024-05-15    Jan-25   JKM      12.50
2024-05-15    Jan-25   Brent    75.00
2024-05-16    Jan-25   JKM      12.75
2024-05-16    Jan-25   Brent    76.00
```

**Processing**:
1. Group by period: `period = 'Jan-25'`
2. No averaging (monthly mode returns raw data)
3. Merge on `[trade_date, strip]`
4. Calculate slopes:
   - 2024-05-15: (12.50 / 75.00) × 100 = 16.67%
   - 2024-05-16: (12.75 / 76.00) × 100 = 16.78%

**Output**:
```
trade_date    strip    slope_percentage
2024-05-15    Jan-25   16.67
2024-05-16    Jan-25   16.78
```

### Example 2: Calendar Grouping
**Input Data**:
```
trade_date    strip    product  settlement_price
2024-05-15    Jan-25   JKM      12.50
2024-05-15    Feb-25   JKM      12.75
2024-05-15    Mar-25   JKM      13.00
2024-05-15    Jan-25   Brent    75.00
2024-05-15    Feb-25   Brent    76.00
2024-05-15    Mar-25   Brent    77.00
```

**Processing**:
1. Parse maturity_date: Jan-25 → 2025-01-01, Feb-25 → 2025-02-01, etc.
2. Group by period: `period = '2025'`
3. Aggregate by `[product, trade_date, period]`:
   - JKM 2025: (12.50 + 12.75 + 13.00) / 3 = 12.75
   - Brent 2025: (75.00 + 76.00 + 77.00) / 3 = 76.00
4. Merge on `[trade_date, strip='2025']`
5. Calculate slope: (12.75 / 76.00) × 100 = 16.78%

**Output**:
```
trade_date    strip    slope_percentage
2024-05-15    2025     16.78
```

### Example 3: With JKM Discount
**Input**:
- JKM Price: $12.50/MMBtu
- Brent Price: $75.00/bbl
- JKM Discount: $2.00/MMBtu

**Processing**:
1. Apply discount: JKM = 12.50 - 2.00 = 10.50
2. Calculate slope: (10.50 / 75.00) × 100 = 14.00%

**Output**:
```
slope_percentage: 14.00%
jkm_price: 10.50 (discounted from 12.50)
brent_price: 75.00
```

---

## 10. Technical Implementation Notes

### 10.1 Callback Structure
The page uses Dash callbacks with the following dependency graph:

```
refresh-trigger (Store)
    ↓
    ├─→ slope-from-date-picker (DatePickerSingle)
    ├─→ slope-comparison-date-picker (DatePickerSingle)
    └─→ Data refresh in global df_options

User Inputs:
    ├─→ slope-from-date-picker
    ├─→ slope-comparison-date-picker
    ├─→ slope-grouping-dropdown
    ├─→ slope-product-dropdown
    └─→ jkm-discount-input
            ↓
            ├─→ update_slope_graphs()
            └─→ update_slope_tables()
```

### 10.2 Performance Considerations
1. **Global DataFrame**: `df_options` is stored at module level to avoid reloading on every callback
2. **Inner Join**: Reduces data volume by keeping only matching pairs
3. **Groupby Aggregation**: Pre-aggregates before slope calculation
4. **Date Filtering**: Applied early in pipeline to reduce processing volume

### 10.3 Error Handling
- **Empty DataFrames**: Returns user-friendly messages
- **Missing Dates**: Uses `errors='coerce'` in datetime parsing
- **Division by Zero**: Protected by data validation (Brent price > 0)
- **No Matches**: Inner join returns empty if no matches found

---

## 11. Future Enhancements

### 11.1 Additional Slope Ratios
- TTF/Brent (European gas to oil)
- Henry Hub/Brent (US gas to oil)
- JKM/TTF (Asian to European gas)
- Custom user-defined ratios

### 11.2 Advanced Grouping
- Rolling windows (30-day, 90-day averages)
- Custom date ranges
- Forward curve segments (prompt, 1-year, 2-year strips)

### 11.3 Statistical Analysis
- Percentile bands (P10, P50, P90)
- Z-scores and outlier detection
- Correlation analysis
- Regression models

### 11.4 Export Capabilities
- CSV download
- Excel export with multiple sheets
- PDF report generation
- API endpoints for programmatic access

---

## 12. Glossary

| Term | Definition |
|------|------------|
| **Slope** | Ratio of JKM to Brent prices, expressed as a percentage |
| **Strip** | A contract or set of contracts for a specific delivery period |
| **Maturity Date** | The delivery date/period for a futures contract |
| **Trade Date** | The date when a contract price is observed/traded |
| **Expiration Date** | The last date a futures contract can be traded |
| **Settlement Price** | Official daily closing price for a futures contract |
| **JKM** | Japan Korea Marker - Asian LNG price benchmark |
| **Brent** | Brent Crude Oil - Global oil price benchmark |
| **Brent Spot** | Current Brent futures price (contract 'B' or 'Brent') |
| **Brent 301 (B31)** | 3-month lag, 1-month average Brent index (M-3) |
| **Brent 303 (B33)** | 3-month lag, 3-month average Brent index (M-5 to M-3) |
| **Brent 601 (B61)** | 6-month lag, 1-month average Brent index (M-6) |
| **Brent 603 (B63)** | 6-month lag, 3-month average Brent index (M-8 to M-6) |
| **Lagged Index** | Brent price calculated from historical averages with time delay |
| **Averaging Window** | Period of months used to calculate average Brent price |
| **Lag Months** | Number of months before delivery to look back for pricing |
| **Calendar Strip** | All monthly contracts within a calendar year |
| **Season Strip** | Contracts grouped by thermal season (Summer/Winter) |
| **Synthetic Row** | Calculated data row (e.g., lagged Brent index) not from raw database |

---

## Document Information
- **Version**: 2.0
- **Last Updated**: 2025-10-16
- **Author**: System Documentation
- **File**: `/home/efernandez/development/Github/dash_options/pages/SLOPES_METHODOLOGY.md`
- **Changelog**:
  - **v2.0** (2025-10-16): Added Brent lagged indices (301, 303, 601, 603) with full methodology, calculation examples, and user controls documentation
  - **v1.0** (2025-10-16): Initial documentation of slopes page methodology
