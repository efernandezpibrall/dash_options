# pages/greeks.py
import configparser
from dash import html, dcc, dash_table, callback, Output, Input, State, Dash, html, dcc, ALL
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
import plotly.express as px
import dash
from dash.dash_table.Format import Format, Group, Scheme
import os
import json
from dash import Dash, html, dcc, dash_table, Input, Output, callback_context
import pandas as pd
import psycopg2
import requests # Add this import
import datetime as dt
import plotly.express as px
from sqlalchemy import create_engine

#------ code to be able to access config.ini, even having the path in the .virtualenvs is not working without it ------#
try:
    # Get the directory where your script is located
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # Navigate to the directory containing config.ini
    # Adjust the number of '..' as needed to reach the correct directory
    config_dir = os.path.abspath(os.path.join(script_dir, '..','..'))  # Go up one level
    CONFIG_FILE_PATH = os.path.join(config_dir, 'config.ini')
except:
    CONFIG_FILE_PATH = 'config.ini'  # Assumes it's in the same directory or the path it is detected


# --- Load Configuration from INI File ---
config_reader = configparser.ConfigParser(interpolation=None)
config_reader.read(CONFIG_FILE_PATH)

# Read values from the ini file sections
DB_CONNECTION_STRING = config_reader.get('DATABASE', 'CONNECTION_STRING', fallback=None)
DB_SCHEMA = config_reader.get('DATABASE', 'SCHEMA', fallback='at_lng')
PROXY_URL = config_reader.get('NETWORK', 'PROXY_URL', fallback=None)
ASPECT_USERNAME = config_reader.get('ASPECT', 'USERNAME', fallback=None)
ASPECT_PASSWORD = config_reader.get('ASPECT', 'PASSWORD', fallback=None)

# --- Essential Variable Checks ---
if not DB_CONNECTION_STRING:
    raise ValueError(f"Missing DATABASE CONNECTION_STRING in {CONFIG_FILE_PATH}")

# create engine
engine = create_engine(DB_CONNECTION_STRING, pool_pre_ping=True)

def aspect_live_exposure_report(cob, username, password):
    # Get today's date
    today = dt.date.today()
    # Convert cob to date object if it's a string
    if isinstance(cob, str):
        cob_date = dt.datetime.strptime(cob, "%Y-%m-%d").date()
    else:
        cob_date = cob
    # Set todayPricing based on whether cob is today or before today
    if cob_date == today:
        today_pricing = 0  # yes
    else:
        today_pricing = 1  # no
    param = {}
    url = 'https://at.myaspect.net/webservice/aspectrs/_MiddleOffice_POSITION_ExposureReport'
    cobdate = {
        "cobDate": cob,
        "todayPricing": today_pricing,
        "book": 'AD-LNG',
        'displayExchangeTradeOtc': True
    }
    proxies = {'http': PROXY_URL, 'https': PROXY_URL} if PROXY_URL else None
    json_cobdate = json.dumps(cobdate)
    aspect_call = requests.post(
        url,
        data=json_cobdate,
        auth=(username, password),
        params=param,
        verify=False,
        proxies=proxies
    )
    result = pd.DataFrame.from_records(aspect_call.json())
    return result


# Available Greeks
GREEK_OPTIONS = [
    {'label': 'Delta', 'value': 'delta'},
    {'label': 'Gamma', 'value': 'gamma'},
    {'label': 'Vega', 'value': 'vega'},
    {'label': 'Theta', 'value': 'theta'},
    {'label': 'Correlation', 'value': 'correlation'}
]


# ---------------------- Data Functions ----------------------
def fetch_options_valuation_data(engine, cob_date=None):
    try:
        query = f"""
        SELECT *
        FROM at_lng.trades_options_valuation
        WHERE cob_date = '{cob_date}'
        """

        return pd.read_sql(query, engine)
    except Exception as e:
        print(f"Failed to fetch options data: {str(e)}")
        return pd.DataFrame()


def mapping_instrument_units(engine, cob_date=None):
    try:
        query = f"""
        SELECT a.instrument,b."Conv_factor", b."To_unit" as "unit"
        FROM {DB_SCHEMA}.mapping_instrument_marker a
        left join at_lng.mapping_instrument_curve_enverus b
        on a."Marker"=b."Instrument"
        """

        return pd.read_sql(query, engine)
    except Exception as e:
        print(f"Failed to fetch options data: {str(e)}")
        return pd.DataFrame()


def get_available_dates(engine):
    """Get list of available trade dates."""
    try:
        query = "SELECT DISTINCT cob_date FROM at_lng.trades_options_valuation ORDER BY cob_date DESC"
        dates = pd.read_sql(query, engine)
        return dates['cob_date'].tolist()
    except Exception as e:
        print(f"Failed to fetch available dates: {str(e)}")
        return []


def get_strategies(engine, cob_date):
    """Get list of available strategies and assets for a given date."""
    try:
        query = f"""
        SELECT DISTINCT substrategy
        FROM at_lng.trades_options_valuation
        WHERE cob_date = '{cob_date}'
        """
        data = pd.read_sql(query, engine)
        return sorted(data['substrategy'].unique())
    except Exception as e:
        print(f"Failed to fetch strategies and assets: {str(e)}")
        return []


# First, add a function to get available trade types
def get_trade_types(engine, cob_date):
    """Get list of available trade types for a given date."""
    try:
        query = f"""
        SELECT DISTINCT type_trade
        FROM at_lng.trades_options_valuation
        WHERE cob_date = '{cob_date}'
        """
        data = pd.read_sql(query, engine)
        return sorted(data['type_trade'].unique())
    except Exception as e:
        print(f"Failed to fetch trade types: {str(e)}")
        return []


# Function to create consistent asset pair naming
def create_standardized_asset_pair(asset_a, asset_b):
    """
    Create a standardized asset pair name by alphabetically ordering the assets.
    This ensures that 'ICE_HH-ICE_JKM' and 'ICE_JKM-ICE_HH' are treated as the same pair.
    Args:
        asset_a: First asset name
        asset_b: Second asset name
    Returns:
        Standardized asset pair name
    """
    # Sort the assets alphabetically to ensure consistent naming
    assets = sorted([asset_a, asset_b])
    return f"{assets[0]}-{assets[1]}"


def create_greeks_pivot_table(data, greek_type, lots_enabled=False):
    """
    Create a pivot table with maturity dates as rows and assets as columns.
    When maturity_date_type is 'calendar', spread the greeks evenly across all 12 months of the year.
    For correlation, preserves calendar labels without monthly splitting.
    Uses mapping_instrument_units for unit conversion.

    Args:
        data: DataFrame containing the Greek data
        greek_type: Type of Greek to calculate (delta, gamma, vega, theta, correlation)
        lots_enabled: Boolean indicating whether to convert to lots
    """
    # Create a copy of the data
    df = data.copy()

    # Handle correlation differently
    if greek_type == 'correlation':
        return create_correlation_pivot_table(df)

    # Map Greek type to corresponding columns
    greek_columns = {
        'delta': ['qty_delta_asset_a', 'qty_delta_asset_b'],
        'gamma': ['qty_gamma_asset_a', 'qty_gamma_asset_b'],
        'vega': ['qty_vega_sigma1', 'qty_vega_sigma2'],
        'theta': ['qty_theta'],
        'correlation': ['qty_corr_sensitivity']
    }

    # Get instrument to unit mapping and conversion factors
    instrument_mapping_df = mapping_instrument_units(engine)

    # Create a dictionary for quick lookups
    unit_conv_map = {}
    if not instrument_mapping_df.empty:
        for _, row in instrument_mapping_df.iterrows():
            if row['instrument'] and not pd.isna(row['instrument']):
                unit_conv_map[row['instrument']] = {
                    'unit': row['unit'] if not pd.isna(row['unit']) else '',
                    'conv_factor': row['Conv_factor'] if not pd.isna(row['Conv_factor']) else 1.0
                }

    # Separate calendar and month entries
    calendar_entries = df[df['maturity_date_type_a'] == 'calendar'].copy()
    month_entries = df[df['maturity_date_type_a'] != 'calendar'].copy()

    # Create expanded DataFrame to hold all entries including distributed calendar entries
    expanded_entries = []

    # Process month entries normally
    if not month_entries.empty:
        # Ensure maturity_date column exists and is properly formatted
        try:
            # Extract year and month from maturity date
            month_entries['maturity_date'] = month_entries['maturity_date_a'].dt.strftime('%Y-%m')
        except Exception as e:
            print(f"Error formatting maturity date: {e}")
            # Create a default maturity_date if there's an issue
            month_entries['maturity_date'] = "Unknown"

        expanded_entries.append(month_entries)

    # Process calendar entries - distribute across all 12 months
    if not calendar_entries.empty:
        for _, row in calendar_entries.iterrows():
            year = row['maturity_date_a'].year

            # Create 12 copies of this row, one for each month
            for month in range(1, 13):
                month_row = row.copy()
                month_row['maturity_date'] = f"{year}-{month:02d}"

                # Divide all Greek values by 12 to distribute evenly
                for greek_col in [col for sublist in greek_columns.values() for col in sublist]:
                    if greek_col in month_row:
                        month_row[greek_col] = month_row[greek_col] / 12

                expanded_entries.append(pd.DataFrame([month_row]))

    # Combine all entries
    if expanded_entries:
        df_expanded = pd.concat(expanded_entries, ignore_index=True)
    else:
        print("No data to process after calendar/month filtering")
        return pd.DataFrame(), {}  # Return empty DataFrame and empty unit map if no data

    # Create asset-specific Greek values
    result_dfs = []

    # Process asset_a
    df_a = df_expanded.copy()
    df_a['asset'] = df_a['asset_a']

    # Get conversion factor for the asset
    df_a['conv_factor'] = df_a['asset'].map(lambda x: unit_conv_map.get(x, {}).get('conv_factor', 1.0))

    # Get unit for the asset
    df_a['unit'] = df_a['asset'].map(lambda x: unit_conv_map.get(x, {}).get('unit', ''))

    # Apply appropriate calculations based on the greek type and lots setting
    if greek_type == 'delta' or greek_type == 'gamma':
        # Apply conversion factor for delta and gamma
        df_a['greek_value'] = df_a[greek_columns[greek_type][0]] * df_a['conv_factor']

        # Apply lots conversion if enabled for delta/gamma
        if lots_enabled:
            # Apply lots conversion based on the unit
            df_a['greek_value'] = df_a.apply(
                lambda row: row['greek_value'] / 1000 if row['unit'] == 'BBL' else
                row['greek_value'] / 10000 if row['unit'] == 'MMBtu' else
                row['greek_value'],  # No change for 'Day' or other units
                axis=1
            )
    elif greek_type == 'theta':
        # For theta, split the total theta between assets (50% each)
        # No conversion factor for theta
        df_a['greek_value'] = df_a[greek_columns[greek_type][0]] * 0.5
    else:
        # For other Greeks (vega, etc.), no conversion factor needed
        df_a['greek_value'] = df_a[greek_columns[greek_type][0]]

    result_dfs.append(df_a[['maturity_date', 'asset', 'greek_value']])

    # Process asset_b
    df_b = df_expanded.copy()
    df_b['asset'] = df_b['asset_b']

    # Get conversion factor for the asset
    df_b['conv_factor'] = df_b['asset'].map(lambda x: unit_conv_map.get(x, {}).get('conv_factor', 1.0))

    # Get unit for the asset
    df_b['unit'] = df_b['asset'].map(lambda x: unit_conv_map.get(x, {}).get('unit', ''))

    # Apply appropriate calculations based on the greek type and lots setting
    if greek_type == 'delta' or greek_type == 'gamma':
        # Apply conversion factor for delta and gamma
        df_b['greek_value'] = df_b[greek_columns[greek_type][1]] * df_b['conv_factor']

        # Apply lots conversion if enabled for delta/gamma
        if lots_enabled:
            # Apply lots conversion based on the unit
            df_b['greek_value'] = df_b.apply(
                lambda row: row['greek_value'] / 1000 if row['unit'] == 'BBL' else
                row['greek_value'] / 10000 if row['unit'] == 'MMBtu' else
                row['greek_value'],  # No change for 'Day' or other units
                axis=1
            )
    elif greek_type == 'theta':
        # For theta, split the total theta between assets (50% each)
        # No conversion factor for theta
        df_b['greek_value'] = df_b[greek_columns[greek_type][0]] * 0.5
    else:
        # For other Greeks (vega, etc.), no conversion factor needed
        df_b['greek_value'] = df_b[greek_columns[greek_type][1]]

    result_dfs.append(df_b[['maturity_date', 'asset', 'greek_value']])

    # Combine results
    combined_df = pd.concat(result_dfs)

    # Group by maturity date and asset, sum up the Greek values
    grouped_df = combined_df.groupby(['maturity_date', 'asset'])['greek_value'].sum().reset_index()

    # Create the pivot table
    pivot_df = grouped_df.pivot(index='maturity_date', columns='asset', values='greek_value')

    # Replace NaN with 0
    pivot_df = pivot_df.fillna(0)

    # Sort by maturity date
    pivot_df = pivot_df.sort_index()

    # Add row totals
    pivot_df['Row Total'] = pivot_df.sum(axis=1)

    # Calculate column totals
    totals_row = pivot_df.sum().to_frame().T
    totals_row.index = ['Total']

    # Combine the pivot table with the totals row
    final_df = pd.concat([pivot_df, totals_row])

    # Convert all values to integers for better readability
    final_df = final_df.astype(int)

    # Build unit map using the instrument_mapping_df
    unit_map = {}
    for asset in pivot_df.columns:
        if asset != 'Row Total':
            unit_info = unit_conv_map.get(asset, {})
            unit_map[asset] = unit_info.get('unit', '')

    return final_df, unit_map


def aggregate_by_date_level(data, date_column, aggregation_level):
    """
    Aggregates data based on selected date aggregation level.
    Args:
        data: DataFrame with date column
        date_column: Name of the date column
        aggregation_level: 'month', 'quarter', or 'year'
    Returns:
        DataFrame with aggregated dates
    """
    # Create a copy to avoid modifying the original
    df = data.copy()
    # Make sure the date column is in the right format
    # First check if we need to handle special cases like correlation with maturity_pair
    if date_column == 'maturity_pair' and '/' in str(df[date_column].iloc[0]):
        # This is a correlation table with maturity pairs
        # We'll need to handle each part of the pair separately

        # Create a function to process each date part in the pair
        def process_maturity_pair(pair_str):
            if '/' not in pair_str:
                # Single date/cal entry
                return process_single_date(pair_str)
            else:
                # Split the pair and process each part
                parts = pair_str.split('/')
                processed_parts = [process_single_date(part) for part in parts]
                return '/'.join(processed_parts)

        # Helper to process a single date or calendar entry
        def process_single_date(date_str):
            # Handle calendar entries
            if '-CAL' in date_str:
                year = date_str.split('-')[0]
                return f"{year}-CAL"  # Calendar entries stay as they are
            # Handle regular dates based on aggregation level
            try:
                date_obj = pd.to_datetime(date_str)
                if aggregation_level == 'month':
                    return date_obj.strftime('%Y-%m')
                elif aggregation_level == 'quarter':
                    return f"{date_obj.year}-Q{(date_obj.month - 1) // 3 + 1}"
                elif aggregation_level == 'year':
                    return str(date_obj.year)
            except:
                return date_str  # If parsing fails, return as is
            return date_str

        # Apply the processing to each maturity pair
        df[date_column] = df[date_column].apply(process_maturity_pair)
    else:
        # Standard date column handling
        try:
            # Check if we're dealing with string dates in YYYY-MM format
            if df[date_column].dtype == 'object':
                # For string date formats like "2023-05"
                def process_string_date(date_str):
                    # Skip 'Total' or other non-date values
                    if not isinstance(date_str, str) or date_str == 'Total':
                        return date_str
                    # Handle YYYY-MM format
                    if '-' in date_str and len(date_str.split('-')) == 2:
                        year, month = date_str.split('-')
                        if aggregation_level == 'month':
                            return date_str  # Keep as is
                        elif aggregation_level == 'quarter':
                            try:
                                month_num = int(month)
                                quarter = (month_num - 1) // 3 + 1
                                return f"{year}-Q{quarter}"
                            except ValueError:
                                return date_str
                        elif aggregation_level == 'year':
                            return year
                    return date_str

                df[date_column] = df[date_column].apply(process_string_date)
            # Try to convert to datetime if not already
            elif not pd.api.types.is_datetime64_any_dtype(df[date_column]):
                # Save the 'Total' row if it exists
                mask_total = df[date_column] == 'Total'
                total_indices = df.index[mask_total]
                # Convert non-Total values to datetime
                if not all(mask_total):
                    df.loc[~mask_total, date_column] = pd.to_datetime(
                        df.loc[~mask_total, date_column],
                        errors='coerce'
                    )
                # Now apply the aggregation based on level
                if aggregation_level == 'month':
                    # Keep as Year-Month
                    df.loc[~mask_total, date_column] = df.loc[~mask_total, date_column].dt.strftime('%Y-%m')
                elif aggregation_level == 'quarter':
                    # Convert to Year-Quarter
                    df.loc[~mask_total, date_column] = df.loc[~mask_total, date_column].apply(
                        lambda x: f"{x.year}-Q{(x.month - 1) // 3 + 1}" if pd.notna(x) else "Unknown"
                    )
                elif aggregation_level == 'year':
                    # Convert to just Year
                    df.loc[~mask_total, date_column] = df.loc[~mask_total, date_column].dt.year.astype(str)
            # For data already in datetime format
            else:
                # Apply formatting to datetime column
                if aggregation_level == 'month':
                    df[date_column] = df[date_column].dt.strftime('%Y-%m')
                elif aggregation_level == 'quarter':
                    df[date_column] = df[date_column].apply(
                        lambda x: f"{x.year}-Q{(x.month - 1) // 3 + 1}" if pd.notna(x) else "Unknown"
                    )
                elif aggregation_level == 'year':
                    df[date_column] = df[date_column].dt.year.astype(str)
        except Exception as e:
            print(f"Error during date processing: {e}")
            # If datetime conversion fails, try string manipulation
            # This handles string date formats or cases where we have special entries
            if aggregation_level == 'quarter' or aggregation_level == 'year':
                def extract_year_and_aggregate(val):
                    # Try to extract year from string like "2023-05" or "2023-CAL"
                    if isinstance(val, str) and '-' in val:
                        year = val.split('-')[0]
                        if aggregation_level == 'year':
                            return year
                        elif aggregation_level == 'quarter':
                            # Try to extract month and convert to quarter
                            parts = val.split('-')
                            if len(parts) >= 2 and parts[1].isdigit():
                                month = int(parts[1])
                                quarter = (month - 1) // 3 + 1
                                return f"{year}-Q{quarter}"
                    return val

                df[date_column] = df[date_column].apply(extract_year_and_aggregate)

    # Group by the processed date column and sum up all numeric columns
    if not df.empty:
        # Save Total row if it exists
        total_row = None
        if 'Total' in df[date_column].values:
            total_row = df[df[date_column] == 'Total']
            df = df[df[date_column] != 'Total']  # Remove total for grouping
        # Get numeric columns excluding the date column
        numeric_cols = [col for col in df.select_dtypes(include=['number']).columns
                        if col != date_column]
        if numeric_cols:
            # Create a different approach to avoid the column collision
            # 1. Rename the date column temporarily
            temp_col_name = f"temp_{date_column}"
            df.rename(columns={date_column: temp_col_name}, inplace=True)
            # 2. Group by the temporary column name
            df_grouped = df.groupby(temp_col_name)[numeric_cols].sum().reset_index()
            # 3. Rename back to the original column name
            df_grouped.rename(columns={temp_col_name: date_column}, inplace=True)
            # Add back the total row if it exists
            if total_row is not None:
                cols_to_use = [date_column] + [col for col in numeric_cols if col in total_row.columns]
                total_subset = total_row[cols_to_use]
                df_grouped = pd.concat([df_grouped, total_subset], ignore_index=True)
            return df_grouped
    # If we can't aggregate, return the original data with processed dates
    if total_row is not None:
        df = pd.concat([df, total_row], ignore_index=True)
    return df


def create_correlation_pivot_table(df):
    """
    Create a pivot table specifically for correlation sensitivity,
    showing asset pairs as columns and maturity date pairs as rows.
    For calendar maturities, preserve the calendar label without splitting.
    Uses standardized asset pair naming to avoid duplicates.
    """
    # Create a copy of the data
    df = df.copy()

    # Create expanded DataFrame to hold all entries
    processed_entries = []
    # Process each row to create the maturity pair labels
    for _, row in df.iterrows():
        try:
            # Determine display format for each asset based on its maturity type
            if row['maturity_date_type_a'] == 'calendar':
                # For calendar type, use the year as label
                year_a = row['maturity_date_a'].year
                mat_label_a = f"{year_a}-CAL"
            else:
                # For month type, use year-month format
                mat_label_a = row['maturity_date_a'].strftime('%Y-%m')
            if row['maturity_date_type_b'] == 'calendar':
                # For calendar type, use the year as label
                year_b = row['maturity_date_b'].year
                mat_label_b = f"{year_b}-CAL"
            else:
                # For month type, use year-month format
                mat_label_b = row['maturity_date_b'].strftime('%Y-%m')
            # Create maturity pair label
            if mat_label_a == mat_label_b:
                # Same maturity type and period for both assets
                row['maturity_pair'] = mat_label_a
            else:
                # Different maturity dates or types
                # Sort maturity labels to maintain consistency when possible
                # But only sort if they're the same format (both calendar or both monthly)
                if ('CAL' in mat_label_a and 'CAL' in mat_label_b) or \
                        ('CAL' not in mat_label_a and 'CAL' not in mat_label_b):
                    maturities = sorted([mat_label_a, mat_label_b])
                    row['maturity_pair'] = f"{maturities[0]}/{maturities[1]}"
                else:
                    # Don't sort if mixing calendar and monthly
                    row['maturity_pair'] = f"{mat_label_a}/{mat_label_b}"
            # Create standardized asset pair name
            row['asset_pair'] = create_standardized_asset_pair(row['asset_a'], row['asset_b'])
            processed_entries.append(row)
        except Exception as e:
            print(f"Error formatting maturity date: {e}")
            row['maturity_pair'] = "Unknown"
            row['asset_pair'] = create_standardized_asset_pair(row['asset_a'], row['asset_b'])
            processed_entries.append(row)
    # Convert processed entries to DataFrame
    if processed_entries:
        df_processed = pd.DataFrame(processed_entries)
    else:
        print("No data to process")
        return pd.DataFrame(), {}  # Return empty DataFrame and empty unit map if no data
    # Select relevant columns and group by maturity pair and asset pair
    corr_df = df_processed[['maturity_pair', 'asset_pair', 'qty_corr_sensitivity']]
    grouped_df = corr_df.groupby(['maturity_pair', 'asset_pair'])['qty_corr_sensitivity'].sum().reset_index()
    # Create the pivot table with asset pairs as columns
    pivot_df = grouped_df.pivot(
        index='maturity_pair',
        columns='asset_pair',
        values='qty_corr_sensitivity'
    )
    # Replace NaN with 0
    pivot_df = pivot_df.fillna(0)

    # Sort by maturity date - custom sorting to handle calendar labels
    def maturity_sort_key(maturity_pair):
        """Custom sort key for maturity pairs including calendars"""
        if '/' in maturity_pair:
            # Split the pair and sort by each component
            mat_a, mat_b = maturity_pair.split('/')
            # Handle calendar labels
            key_a = (mat_a.replace('-CAL', '-13'), mat_a)  # Place calendars after months
            key_b = (mat_b.replace('-CAL', '-13'), mat_b)
            return (key_a, key_b)
        else:
            # Single maturity, might be calendar
            return ((maturity_pair.replace('-CAL', '-13'), maturity_pair),)

    try:
        # Sort with custom key function
        pivot_df = pivot_df.reindex(sorted(pivot_df.index, key=maturity_sort_key))
    except Exception as e:
        print(f"Error sorting maturity pairs: {e}")
        # If sorting fails, leave as is

    # Add row totals
    pivot_df['Row Total'] = pivot_df.sum(axis=1)

    # Calculate column totals
    totals_row = pivot_df.sum().to_frame().T
    totals_row.index = ['Total']
    # Combine the pivot table with the totals row
    final_df = pd.concat([pivot_df, totals_row])
    # Convert all values to integers for better readability
    final_df = final_df.astype(int)

    # Return an empty unit map for correlation
    return final_df, {}


# ---------------------- Visualization Functions ----------------------
def plot_greeks_bar_chart(pivot_df, greek_type):
    """
    Create a bar chart visualization of Greeks exposure.
    Updated for correlation pairs with calendar labels.
    """
    # Remove the Total row for the bar chart
    bar_df = pivot_df.copy()
    if 'Total' in bar_df.index:
        bar_df = bar_df.drop(index=['Total'])

    # Remove the Row Total column for visualization
    if 'Row Total' in bar_df.columns:
        bar_df = bar_df.drop(columns=['Row Total'])

    # Melt the DataFrame for easier plotting
    id_var = 'maturity_pair' if greek_type == 'correlation' else 'maturity_date'

    # For non-correlation, ensure the id_var exists
    if greek_type != 'correlation' and id_var not in bar_df.index.names:
        # Reset index to get columns
        bar_df = bar_df.reset_index()
        # Rename the index column if needed
        if 'index' in bar_df.columns and id_var not in bar_df.columns:
            bar_df = bar_df.rename(columns={'index': id_var})
    else:
        bar_df = bar_df.reset_index()

    melted_df = bar_df.melt(
        id_vars=id_var,
        value_vars=[col for col in bar_df.columns if col != id_var],
        var_name='Asset Pair' if greek_type == 'correlation' else 'Asset',
        value_name='Value'
    )

    # Professional Distinct Color Palette - High Contrast
    professional_colors = [
        '#2E86C1',  # McKinsey blue (primary)
        '#E74C3C',  # Vibrant red
        '#27AE60',  # Forest green
        '#F39C12',  # Orange
        '#9B59B6',  # Purple
        '#1ABC9C',  # Turquoise
        '#34495E',  # Dark gray-blue
        '#F1C40F',  # Bright yellow
        '#E67E22',  # Dark orange
        '#95A5A6',  # Gray
        '#8E44AD',  # Dark purple
        '#16A085'   # Dark turquoise
    ]

    # Create the bar chart
    fig = px.bar(
        melted_df,
        x=id_var,
        y='Value',
        color='Asset Pair' if greek_type == 'correlation' else 'Asset',
        barmode='group',
        color_discrete_sequence=professional_colors,
        height=420  # Standard professional height
    )

    # Professional Chart Styling - Following .claude/commands guidelines
    x_title = 'Maturity Pair' if greek_type == 'correlation' else 'Maturity (YYYY-MM)'
    y_title = 'Correlation Sensitivity' if greek_type == 'correlation' else f'{greek_type.capitalize()} Exposure'
    legend_title = 'Asset Pair' if greek_type == 'correlation' else 'Asset'
    
    fig.update_layout(
        # Professional Title Styling (Empty for section title usage)
        title=dict(
            text='',
            font=dict(size=18, color='#2C3E50', family='Segoe UI, -apple-system, BlinkMacSystemFont, sans-serif')
        ),
        
        # X-Axis Professional Styling (No Title)
        xaxis=dict(
            title=dict(text='', font=dict(size=13, color='#4A4A4A')),  # Empty title
            tickangle=0,  # Horizontal for readability
            showgrid=True,
            gridcolor='rgba(200, 200, 200, 0.3)',  # Subtle grid
            gridwidth=0.5,
            linecolor='#CCCCCC',
            linewidth=1,
            tickfont=dict(size=11, color='#666666'),
            tickmode='auto'
        ),
        
        # Y-Axis Professional Styling
        yaxis=dict(
            title=dict(text=y_title, font=dict(size=13, color='#4A4A4A')),
            showgrid=True,
            gridcolor='rgba(200, 200, 200, 0.3)',
            gridwidth=0.5,
            linecolor='#CCCCCC',
            linewidth=1,
            tickfont=dict(size=11, color='#666666'),
            zeroline=True,
            zerolinecolor='rgba(150, 150, 150, 0.4)',
            zerolinewidth=1
        ),
        
        # Professional Legend Positioning - Right Side (No Title)
        legend=dict(
            title=dict(text='', font=dict(size=12, color='#4A4A4A')),  # Empty title
            orientation='v',  # Vertical layout
            yanchor='middle',
            y=0.5,  # Centered vertically
            xanchor='left',
            x=1.02,  # Right side of chart
            bgcolor='rgba(255, 255, 255, 0)',  # Transparent
            bordercolor='rgba(255, 255, 255, 0)',
            borderwidth=0,
            font=dict(size=11, color='#4A4A4A'),
            itemsizing='constant'
        ),
        
        # Professional Background and Margins
        plot_bgcolor='rgba(248, 249, 250, 0.5)',  # Subtle background
        paper_bgcolor='white',
        margin=dict(l=70, r=140, t=40, b=60),  # More space on right for legend
        
        # Enhanced Interactivity
        hovermode='x unified',
        hoverlabel=dict(
            bgcolor='rgba(255, 255, 255, 0.95)',
            bordercolor='rgba(200, 200, 200, 0.8)',
            font=dict(size=11, color='#2C3E50'),
            align='left'
        ),
    )

    # If there are many asset pairs, keep right-side legend but add scrolling
    if greek_type == 'correlation' and len(bar_df.columns) > 8:
        fig.update_layout(
            legend=dict(
                title=dict(text=''),  # No title for many items case too
                orientation="v",  # Keep vertical for many items
                yanchor="top",
                y=1.0,  # Top alignment for many items
                xanchor="left",
                x=1.02,
                bgcolor='rgba(255, 255, 255, 0.9)',  # Slight background for many items
                bordercolor='rgba(200, 200, 200, 0.3)',
                borderwidth=1
            )
        )

    return fig


# ---------------------- Dash App Setup ----------------------

# Initialize database connection
available_dates = get_available_dates(engine)

# Initialize empty lists for filters
all_strategies = []
all_trade_types = []

# Try to load initial data to populate filters
if available_dates:
    all_strategies = get_strategies(engine, available_dates[0])
    all_trade_types = get_trade_types(engine, available_dates[0])
# ---------------------- App Layout ----------------------
# Improved layout with better aesthetics - replace only the layout portion of your code
# Full-width layout with filters at the top
# Layout with Lots background aligned with Greek Type dropdown
layout = html.Div([
    # Store for Aspect data
    dcc.Store(id='aspect-data-store', storage_type='memory'),
    # Store for refresh trigger
    dcc.Store(id='refresh-trigger-store', storage_type='memory'),
    # Download components for custom export
    dcc.Download(id="download-exposure-data"),
    dcc.Download(id="download-units-data"),

    # Top section with dropdowns on the left and buttons on the right
    html.Div([
        # Left side - Dropdowns section with two rows
        html.Div([
            # First row - Main controls
            html.Div([
                html.Label('Trade Date:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='date-selector',
                    options=[
                        {
                            'label': pd.to_datetime(date).strftime('%Y-%m-%d') if pd.notna(date) else str(date), 
                            'value': date
                        } for date in available_dates
                    ],
                    value=available_dates[0] if available_dates else None,
                    clearable=False,
                    className="inline-dropdown-date"
                ),
                html.Label('Greek Type:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='greek-selector',
                    options=GREEK_OPTIONS,
                    value='delta',
                    clearable=False,
                    className="inline-dropdown-greek"
                ),
                html.Label('Aggregation:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='date-aggregation-selector',
                    options=[
                        {'label': 'Month', 'value': 'month'},
                        {'label': 'Quarter', 'value': 'quarter'},
                        {'label': 'Year', 'value': 'year'}
                    ],
                    value='month',
                    clearable=False,
                    className="inline-dropdown-aggregation"
                ),
                html.Label('Lots:', className="inline-filter-label"),
                dcc.RadioItems(
                    id='lots-toggle',
                    options=[
                        {'label': 'Off', 'value': False},
                        {'label': 'On', 'value': True}
                    ],
                    value=False,
                    inline=True,
                    style={'display': 'flex', 'gap': '10px', 'align-items': 'center'}
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'margin-bottom': '8px'}),
            
            # Second row - Multi-select dropdowns (forced to second row)
            html.Div([
                html.Label('Strategies:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='strategy-selector',
                    options=[{'label': s, 'value': s} for s in all_strategies],
                    value=all_strategies,
                    multi=True,
                    placeholder='Select strategies...',
                    className="inline-dropdown-multi-strategies"
                ),
                html.Label('Trade Types:', className="inline-filter-label"),
                dcc.Dropdown(
                    id='trade-type-selector',
                    options=[{'label': t, 'value': t} for t in all_trade_types],
                    value=all_trade_types,
                    multi=True,
                    placeholder='Select trade types...',
                    className="inline-dropdown-multi"
                )
            ], style={'display': 'flex', 'align-items': 'center', 'gap': '16px', 'flex-wrap': 'nowrap', 'width': '100%'})
        ], className="inline-section-header", style={'flex': '1'}),
        
        # Right side - Action buttons with matching styling
        html.Div([
            html.H3('Aspect Exposure', className="greeks-title"),
            html.Button('Refresh Settlement', id='refresh-button', className='inline-button-primary'),
            html.Button('Refresh Live', id='refresh-live-button', className='inline-button-secondary', style={'background-color': '#28a745', 'color': 'white', 'border': 'none'})
        ], className="inline-section-header", style={'flex': '0 0 auto', 'margin-left': '20px', 'flex-direction': 'column', 'align-items': 'flex-start', 'gap': '8px'})
    ], style={'display': 'flex', 'align-items': 'stretch', 'gap': '20px'}),
    # Professional Content Area
    dcc.Loading(
        id="loading-indicator",
        type="circle",
        children=[
            # Section header according to .claude/commands guidelines
            html.H3('Options Greeks Exposure Analysis', id='chart-section-title', className="greeks-title"),
            # Chart without container wrapper
            dcc.Graph(
                id='greeks-bar-chart',
                config={
                    'displayModeBar': True,
                    'responsive': True,
                    'modeBarButtonsToRemove': ['lasso2d', 'select2d']
                },
                style={'height': '420px', 'margin-bottom': '24px'}
            ),
            # Tables with individual section headers and export buttons - side by side layout
            html.Div([
                # Left side - Exposure Data (60% width)
                html.Div([
                    html.Div([
                        html.H3('Exposure Data', className="greeks-title", style={'margin': '0', 'display': 'inline-block'}),
                        html.Button('Export', id='export-exposure-btn', 
                                   className='custom-export-btn',
                                   style={'margin-left': '12px', 'font-size': '11px', 'padding': '4px 8px', 
                                         'background-color': '#2E86C1', 'color': 'white', 'border': 'none', 
                                         'border-radius': '3px', 'cursor': 'pointer', 'font-weight': '500'})
                    ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '12px'}),
                    html.Div(id='pivot-table-container', className="options-table-container", 
                            style={'border': 'none', 'overflow': 'visible'})
                ], style={'width': '60%', 'margin-right': '20px', 'box-sizing': 'border-box'}),
                # Right side - Unit Aggregation (40% width)
                html.Div([
                    html.Div([
                        html.H3('Unit Aggregation', className="greeks-title", style={'margin': '0', 'display': 'inline-block'}),
                        html.Button('Export', id='export-units-btn', 
                                   className='custom-export-btn',
                                   style={'margin-left': '12px', 'font-size': '11px', 'padding': '4px 8px', 
                                         'background-color': '#2E86C1', 'color': 'white', 'border': 'none', 
                                         'border-radius': '3px', 'cursor': 'pointer', 'font-weight': '500'})
                    ], style={'display': 'flex', 'align-items': 'center', 'margin-bottom': '12px'}),
                    html.Div(id='units-table-container', className="options-table-container",
                            style={'border': 'none', 'overflow': 'visible'})
                ], style={'width': '40%', 'box-sizing': 'border-box'})
            ], style={'display': 'flex', 'flex-wrap': 'nowrap', 'width': '100%', 'align-items': 'flex-start'}),
        ],
        style={'width': '100%'}
    ),
    # Hidden div for storing the processed data
    html.Div(id='processed-data', style={'display': 'none'})
], className="options-dashboard-container")


# ---------------------- Callbacks ----------------------

# Callback to trigger data refresh from navigation bar
@callback(
    Output('refresh-trigger-store', 'data'),
    Input('refresh-options-data', 'n_clicks'),
    prevent_initial_call=True
)
def trigger_data_refresh(n_clicks):
    """Trigger refresh of all PostgreSQL data when navigation refresh button is clicked"""
    if n_clicks:
        print("Refreshing PostgreSQL options data...")
        # Return timestamp to trigger dependent callbacks
        return {'timestamp': pd.Timestamp.now().isoformat(), 'refresh_count': n_clicks}
    return dash.no_update

# Callback to fetch Aspect data when refresh button is clicked
@callback(
    Output('aspect-data-store', 'data'),
    Input('refresh-button', 'n_clicks'),
    Input('refresh-live-button', 'n_clicks'),
    State('date-selector', 'value'),
    prevent_initial_call=True  # Prevent callback on initial load
)
def fetch_aspect_data(refresh_clicks, refresh_live_clicks, selected_date):
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update

    # Determine which button was clicked
    triggered_id = ctx.triggered[0]['prop_id'].split('.')[0]

    if triggered_id == 'refresh-button':
        if not refresh_clicks or not selected_date:
            return dash.no_update

        # Use selected date from dropdown
        if isinstance(selected_date, str):
            date_obj = pd.to_datetime(selected_date)
            date_str = date_obj.strftime('%Y-%m-%d')
        else:
            date_str = selected_date.strftime('%Y-%m-%d')

        print(f"Fetching Aspect data for selected date: {date_str}...")

    elif triggered_id == 'refresh-live-button':
        if not refresh_live_clicks:
            return dash.no_update

        # Use today's date
        today = pd.Timestamp.now().strftime('%Y-%m-%d')
        date_str = today
        print(f"Fetching Aspect data for today's date: {date_str}...")

    else:
        return dash.no_update

    # Fetch the data from the slow API
    try:
        aspect_data = aspect_live_exposure_report(date_str, ASPECT_USERNAME, ASPECT_PASSWORD)
        print("Aspect data fetch complete")
        # Convert to a format that can be stored in dcc.Store (JSON serializable)
        return aspect_data.to_dict('records')
    except Exception as e:
        print(f"Failed to fetch aspect data: {str(e)}")
        return []


@callback(
    # Strategy dropdown outputs
    Output('strategy-selector', 'options'),
    Output('strategy-selector', 'value'),
    # Trade type dropdown outputs
    Output('trade-type-selector', 'options'),
    Output('trade-type-selector', 'value'),
    # Inputs
    Input('date-selector', 'value'),
    Input('aspect-data-store', 'modified_timestamp'),  # triggers updates
    Input('refresh-trigger-store', 'data'),  # triggers refresh from navigation
    # State
    State('aspect-data-store', 'data')
)
def update_strategy_and_trade_types(selected_date, aspect_data_timestamp, refresh_trigger, aspect_data):
    # Default returns in case something goes wrong
    empty_result = ([], [], [], [])

    if not selected_date:
        return empty_result

    # Check if refresh was triggered from navigation
    if refresh_trigger:
        print(f"Strategy/Trade types refresh triggered from navigation bar: {refresh_trigger}")

    try:
        # 1) Fetch all strategies from the DB
        query_strategies = f"""
            SELECT DISTINCT substrategy
            FROM at_lng.trades_options_valuation
            WHERE cob_date = '{selected_date}'
        """
        db_strategies_df = pd.read_sql(query_strategies, engine)
        db_strategies = sorted(db_strategies_df['substrategy'].unique())

        # 2) Fetch all trade types from the DB
        query_trade_types = f"""
            SELECT DISTINCT type_trade
            FROM at_lng.trades_options_valuation
            WHERE cob_date = '{selected_date}'
        """
        db_trade_types_df = pd.read_sql(query_trade_types, engine)
        db_trade_types = sorted(db_trade_types_df['type_trade'].unique())

        # 3) Merge in aspect data if it exists
        if aspect_data:
            df_aspect = pd.DataFrame(aspect_data)

            # Merge in strategies
            if 'strategy' in df_aspect.columns:
                aspect_strategies = sorted(df_aspect['strategy'].unique())
                all_strategies = sorted(set(db_strategies) | set(aspect_strategies))
            else:
                all_strategies = db_strategies

            # Merge in trade types
            if 'entityType' in df_aspect.columns:
                aspect_trade_types = sorted(df_aspect['entityType'].unique())
                all_trade_types = sorted(set(db_trade_types) | set(aspect_trade_types))
            else:
                all_trade_types = db_trade_types
        else:
            # If no aspect data, just use DB results
            all_strategies = db_strategies
            all_trade_types = db_trade_types

        # 4) Build dropdown options and default selections
        strategy_options = [{'label': s, 'value': s} for s in all_strategies]
        trade_type_options = [{'label': t, 'value': t} for t in all_trade_types]

        return (
            strategy_options,  # strategy-selector options
            all_strategies,  # strategy-selector value
            trade_type_options,
            all_trade_types
        )

    except Exception as e:
        print(f"Error updating strategy/trade type dropdowns: {e}")
        return empty_result


# Update the process_data callback to include lots toggle state
@callback(
    Output('processed-data', 'children'),
    Input('date-selector', 'value'),
    Input('strategy-selector', 'value'),
    Input('trade-type-selector', 'value'),
    Input('greek-selector', 'value'),
    Input('lots-toggle', 'value'),
    Input('date-aggregation-selector', 'value'),
    Input('aspect-data-store', 'data'),
    Input('refresh-trigger-store', 'data')
)
def process_data(selected_date, selected_strategies, selected_trade_types, greek_type, lots_enabled, date_aggregation, aspect_data, refresh_trigger):
    if not selected_date or not selected_strategies or not greek_type or not engine:
        return "No data"

    # Check if refresh was triggered from navigation
    if refresh_trigger:
        print(f"Data refresh triggered from navigation bar: {refresh_trigger}")

    # Fetch data
    options_data = fetch_options_valuation_data(engine, selected_date)
    options_data['dealType'] = 'option'

    # Only include Aspect data if available (after button click)
    if aspect_data:
        # Convert aspect_data from dict to DataFrame
        delta_futures = pd.DataFrame(aspect_data)

        if not delta_futures.empty:
            # Check if required columns exist
            required_columns = ['instrument', 'qty', 'uoM', 'maturityForwardDate',
                                'strategy', 'dealType', 'entityType', 'exchangeTradeOtc']

            if all(col in delta_futures.columns for col in required_columns):
                # Process delta_futures
                delta_futures = delta_futures[required_columns].rename(columns={
                    'instrument': 'asset_a',
                    'maturityForwardDate': 'maturity_date_a',
                    'strategy': 'substrategy',
                    'qty': 'quantity',
                    'uoM': 'unit_quantity',
                    'entityType': 'type_trade'
                })
                delta_futures['asset_a_multiplier'] = 1
                delta_futures['asset_a_premium'] = 0
                delta_futures['maturity_date_type_a'] = 'month'
                delta_futures['exchangeTradeOtc'] = 'Physical Option'
                delta_futures['qty_delta_asset_a'] = delta_futures['quantity']
                delta_futures['asset_b'] = delta_futures['asset_a']  # Copy asset_a to asset_b for consistency
                delta_futures['qty_delta_asset_b'] = 0  # No delta for asset_b in this case

                # Convert date columns
                if 'maturity_date_a' in delta_futures.columns:
                    delta_futures['maturity_date_a'] = pd.to_datetime(delta_futures['maturity_date_a'])

                # Add maturity_date_b as a copy of maturity_date_a for consistency
                delta_futures['maturity_date_b'] = delta_futures['maturity_date_a']
                delta_futures['maturity_date_type_b'] = delta_futures['maturity_date_type_a']

                # Combine options_data and delta_futures
                options_data = pd.concat([options_data, delta_futures])

    if options_data.empty:
        return "No data"

    # Convert date columns
    date_columns = ['trade_date', 'maturity_date_a', 'maturity_date_b', 'expiration_date']
    for col in date_columns:
        if col in options_data.columns:
            options_data[col] = pd.to_datetime(options_data[col])

    # Apply filters
    filtered_data = options_data[options_data['substrategy'].isin(selected_strategies)]
    filtered_data = filtered_data[filtered_data['type_trade'].isin(selected_trade_types)]

    if filtered_data.empty:
        return "No data"

    # Create the pivot table - pass the lots_enabled parameter
    pivot_df, unit_map = create_greeks_pivot_table(filtered_data, greek_type, lots_enabled)

    # Make sure the index is named correctly before converting to JSON
    pivot_df = pivot_df.reset_index()

    # Ensure the first column is consistently named for both standard Greeks and correlation
    first_col = pivot_df.columns[0]
    if greek_type == 'correlation':
        pivot_df.rename(columns={first_col: 'maturity_pair'}, inplace=True)
    else:
        pivot_df.rename(columns={first_col: 'maturity_date'}, inplace=True)

    # Store both the pivot table and the unit map and lots information
    result = {
        'pivot_data': pivot_df.to_dict('records'),
        'columns': pivot_df.columns.tolist(),
        'unit_map': unit_map,
        'lots_enabled': lots_enabled,
        'date_aggregation': date_aggregation  # Include the date aggregation setting
    }

    # Return as JSON
    return json.dumps(result)


# Update visualizations with the processed data
@callback(
    Output('greeks-bar-chart', 'figure'),
    Output('pivot-table-container', 'children'),
    Output('units-table-container', 'children'),
    Output('chart-section-title', 'children'),
    Input('processed-data', 'children'),
    Input('greek-selector', 'value'),
    Input('date-aggregation-selector', 'value')
)
def update_visualizations(json_data, greek_type, date_aggregation):
    if not json_data or not greek_type:
        # Create empty figure and empty divs as default returns
        empty_fig = px.bar(title="No data available")
        empty_table = html.Div("No data available")
        empty_units_table = html.Div("No data available")
        empty_title = "Options Greeks Exposure Analysis"
        return (empty_fig, empty_table, empty_units_table, empty_title)

    # Parse the JSON data
    try:
        result = json.loads(json_data)
        data_records = result['pivot_data']
        columns = result['columns']
        unit_map = result['unit_map']
        lots_enabled = result.get('lots_enabled', False)  # Get lots_enabled state
        stored_date_agg = result.get('date_aggregation', 'month')

        # Use the latest setting (either from the dropdown or stored in processed data)
        # If they're different, we need to reaggregate
        need_reaggregation = date_aggregation != stored_date_agg
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error parsing JSON data: {e}")
        # Create empty figure and empty divs as default returns
        empty_fig = px.bar(title="Error parsing data")
        empty_table = html.Div("Error parsing data")
        empty_units_table = html.Div("Error parsing data")
        empty_title = "Options Greeks Exposure Analysis"
        return (empty_fig, empty_table, empty_units_table, empty_title)

    # Convert to DataFrame
    data = pd.DataFrame(data_records)

    if data.empty:
        # Create empty figure and empty divs as default returns
        empty_fig = px.bar(title="No data available")
        empty_table = html.Div("No data available")
        empty_units_table = html.Div("No data available")
        empty_title = "Options Greeks Exposure Analysis"
        return (empty_fig, empty_table, empty_units_table, empty_title)

    # Set the appropriate index column based on Greek type
    index_col = 'maturity_pair' if greek_type == 'correlation' else 'maturity_date'

    # # Apply date aggregation if needed
    # if need_reaggregation:
    #     print(f"Reaggregating data from {stored_date_agg} to {date_aggregation}")
    #     # Apply the date aggregation function
    data = aggregate_by_date_level(data, index_col, date_aggregation)

    if index_col in data.columns:
        pivot_df = data.set_index(index_col)
    else:
        # Fallback to first column if the expected column isn't found
        pivot_df = data.set_index(data.columns[0])

    # Create visualizations
    bar_fig = plot_greeks_bar_chart(pivot_df, greek_type)

    # Add a title that indicates the date aggregation level
    level_display = {
        'month': 'by Month',
        'quarter': 'by Quarter',
        'year': 'by Year'
    }.get(date_aggregation, '')

    # Chart title already set to empty in professional styling

    # Create columns for DataTable - use single-level headers with units included in the names
    table_columns = []

    # Add the maturity/date column first
    table_columns.append({
        "name": index_col,
        "id": index_col
    })

    # Create a dictionary to categorize columns by their unit
    # Structure: {unit: {column_name: column_index, ...}, ...}
    columns_by_unit = {}

    # We'll also need to track which columns belong to which units for later aggregation
    column_unit_mapping = {}

    # Add columns for each asset with their units incorporated in the name
    for col in data.columns:
        if col != index_col and col != 'Row Total':
            # For regular Greeks (not correlation), include the unit in the column name
            if greek_type != 'correlation' and col in unit_map and unit_map[col]:
                unit_display = unit_map[col]
                # Add "(Lots)" to the unit display if lots enabled for delta/gamma
                if lots_enabled and (greek_type == 'delta' or greek_type == 'gamma'):
                    display_name = f"{col} ({unit_display}, Lots)"
                    unit_key = f"{unit_display} (Lots)"
                else:
                    display_name = f"{col} ({unit_display})"
                    unit_key = unit_display

                # Track which unit this column belongs to
                column_unit_mapping[col] = unit_key

                # Initialize the unit in our mapping if it doesn't exist
                if unit_key not in columns_by_unit:
                    columns_by_unit[unit_key] = {}

                # Add this column to the appropriate unit
                columns_by_unit[unit_key][col] = True
            else:
                display_name = col

                # For correlation or items without unit, use "N/A" as unit
                if col != 'Row Total':
                    unit_key = "N/A"
                    column_unit_mapping[col] = unit_key

                    if unit_key not in columns_by_unit:
                        columns_by_unit[unit_key] = {}

                    columns_by_unit[unit_key][col] = True

            table_columns.append({
                "name": display_name,
                "id": col,
                "type": "numeric",
                "format": Format(
                    group=Group.yes,  # Enable grouping (thousand separators)
                    scheme=Scheme.fixed,  # Use fixed precision
                    precision=0,  # No decimal places for integers
                    group_delimiter=',',  # Use comma as thousand separator
                    decimal_delimiter='.'  # Use period as decimal separator
                )
            })

    # Add the Row Total column
    if 'Row Total' in data.columns:
        table_columns.append({
            "name": "Row Total",
            "id": "Row Total",
            "type": "numeric",
            "format": Format(
                group=Group.yes,
                scheme=Scheme.fixed,
                precision=0,
                group_delimiter=',',
                decimal_delimiter='.'
            )
        })

    # Create data table with appropriate header
    table_props = {
        'id': 'pivot-table',
        'columns': table_columns,
        'data': data.to_dict('records'),
        'style_table': {'overflowX': 'auto'},
        'style_cell': {
            'textAlign': 'center',
            'padding': '10px',
            'fontFamily': 'Arial'
        },
        'style_header': {
            'backgroundColor': 'rgb(230, 230, 230)',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'height': 'auto',  # Allow the header to expand to fit content
            'whiteSpace': 'normal',  # Allow line breaks in headers
        },
        'style_data_conditional': [
            {
                'if': {'row_index': len(data) - 1},  # Last row (Total)
                'backgroundColor': 'rgb(230, 230, 230)',
                'fontWeight': 'bold'
            },
            {
                'if': {'column_id': index_col},  # Maturity column
                'fontWeight': 'bold',
                'textAlign': 'left'
            },
            {
                'if': {'column_id': 'Row Total'},  # Row Total column
                'backgroundColor': 'rgb(230, 230, 230)',
                'fontWeight': 'bold'
            }
        ],
        'fill_width': False,
        'export_format': "xlsx"
    }

    table = dash_table.DataTable(**table_props)

    # Create the unit-based table with maturity dates as rows and units as columns
    # First, create a new DataFrame for the unit aggregation
    unit_df = pd.DataFrame(index=pivot_df.index)

    # Set of all units
    all_units = set(column_unit_mapping.values())

    # For each unit, sum up all columns that belong to that unit
    for unit in all_units:
        # Get all columns for this unit
        unit_columns = [col for col, unit_name in column_unit_mapping.items() if unit_name == unit]

        # Sum these columns to get the unit total for each maturity date
        if unit_columns:
            unit_df[unit] = pivot_df[unit_columns].sum(axis=1)
        else:
            unit_df[unit] = 0

    # Reset index to get the maturity date as a column
    unit_df = unit_df.reset_index()

    # Create columns for the units table
    unit_table_columns = []

    # Add the maturity date column first
    unit_table_columns.append({
        "name": index_col,
        "id": index_col
    })

    # Add columns for each unit
    for unit in sorted(all_units):
        unit_table_columns.append({
            "name": unit,
            "id": unit,
            "type": "numeric",
            "format": Format(
                group=Group.yes,
                scheme=Scheme.fixed,
                precision=0,
                group_delimiter=',',
                decimal_delimiter='.'
            )
        })

    # Create the units table props
    units_table_props = {
        'id': 'units-table',
        'columns': unit_table_columns,
        'data': unit_df.to_dict('records'),
        'style_table': {'overflowX': 'auto'},
        'style_cell': {
            'textAlign': 'center',
            'padding': '10px',
            'fontFamily': 'Arial'
        },
        'style_header': {
            'backgroundColor': 'rgb(230, 230, 230)',
            'fontWeight': 'bold',
            'textAlign': 'center',
            'height': 'auto',
            'whiteSpace': 'normal',
        },
        'style_data_conditional': [
            {
                'if': {'row_index': len(unit_df) - 1},  # Last row (Total)
                'backgroundColor': 'rgb(230, 230, 230)',
                'fontWeight': 'bold'
            },
            {
                'if': {'column_id': index_col},  # Maturity column
                'fontWeight': 'bold',
                'textAlign': 'left'
            }
        ],
        'fill_width': False,
        'export_format': "xlsx"
    }

    units_table = dash_table.DataTable(**units_table_props)

    # Create dynamic section title based on Greek type
    dynamic_title = f"Options {greek_type.capitalize()} Exposure Analysis"
    
    return (bar_fig, table, units_table, dynamic_title)


# Custom Export Callbacks
@callback(
    Output("download-exposure-data", "data"),
    Input("export-exposure-btn", "n_clicks"),
    State('processed-data', 'children'),
    State('greek-selector', 'value'),
    prevent_initial_call=True
)
def export_exposure_data(n_clicks, json_data, greek_type):
    """Export Exposure Data table to Excel"""
    if n_clicks and json_data:
        try:
            import io
            result = json.loads(json_data)
            data_records = result['pivot_data']
            df = pd.DataFrame(data_records)
            
            # Set the appropriate index column
            index_col = 'maturity_pair' if greek_type == 'correlation' else 'maturity_date'
            if index_col in df.columns:
                df = df.set_index(index_col)
            
            # Create Excel file in memory
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                df.to_excel(writer, sheet_name='Exposure Data', index=True)
            output.seek(0)
            
            return dcc.send_bytes(output.getvalue(), f"exposure_data_{greek_type}.xlsx")
        except Exception as e:
            print(f"Export error: {e}")
            return None
    return None


@callback(
    Output("download-units-data", "data"),
    Input("export-units-btn", "n_clicks"),
    State('processed-data', 'children'),
    State('greek-selector', 'value'),
    prevent_initial_call=True
)
def export_units_data(n_clicks, json_data, greek_type):
    """Export Unit Aggregation data to Excel"""
    if n_clicks and json_data:
        try:
            import io
            result = json.loads(json_data)
            data_records = result['pivot_data']
            unit_map = result['unit_map']
            
            df = pd.DataFrame(data_records)
            index_col = 'maturity_pair' if greek_type == 'correlation' else 'maturity_date'
            
            if index_col in df.columns:
                pivot_df = df.set_index(index_col)
            else:
                pivot_df = df.set_index(df.columns[0])
            
            # Create unit aggregation similar to the visualization callback
            column_unit_mapping = {}
            for col in df.columns:
                if col != index_col and col != 'Row Total':
                    if greek_type != 'correlation' and col in unit_map and unit_map[col]:
                        unit_key = unit_map[col]
                        column_unit_mapping[col] = unit_key
                    else:
                        column_unit_mapping[col] = "N/A"
            
            unit_df = pd.DataFrame(index=pivot_df.index)
            all_units = set(column_unit_mapping.values())
            
            for unit in all_units:
                unit_columns = [col for col, unit_name in column_unit_mapping.items() if unit_name == unit]
                if unit_columns:
                    unit_df[unit] = pivot_df[unit_columns].sum(axis=1)
                else:
                    unit_df[unit] = 0
            
            unit_df = unit_df.reset_index()
            
            # Create Excel file in memory
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine='openpyxl') as writer:
                unit_df.to_excel(writer, sheet_name='Unit Aggregation', index=False)
            output.seek(0)
            
            return dcc.send_bytes(output.getvalue(), f"unit_aggregation_{greek_type}.xlsx")
        except Exception as e:
            print(f"Export error: {e}")
            return None
    return None

 