import os
import hashlib
import pandas as pd
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import SQLAlchemyError
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DATABASE_URL = f"postgresql://{os.getenv('DB_USER')}:{os.getenv('DB_PASSWORD')}@" \
               f"{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}"

def get_db_connection():
    """Create a database connection."""
    engine = create_engine(DATABASE_URL)
    return engine

def generate_row_hash(row: pd.Series) -> str:
    """Generate a hash for identifying unique rows."""
    columns_to_hash = ["Customer Number", "Invoice Number", "Sales Rep Name", "Revenue Recognition Date", "Extension", "Item Code"]
    row_data = ''.join([str(row[col]) for col in columns_to_hash if col in row]).encode('utf-8')
    return hashlib.sha256(row_data).hexdigest()

def map_novo_to_harmonised():
    """
    Map Novo data from the 'master_novo_sales' table to the harmonised_table structure.
    
    Mapping logic:
      - Join master_novo_sales with commission rates from sales_rep_commission_tier.
      - Calculate:
            "Comm Amount tier 1" = "Commission Amount" * "Commission tier 1 rate"
            "Comm tier 2 diff amount" = ("Commission Amount" * "Commission tier 2 rate") - ("Commission Amount" * "Commission tier 1 rate")
      - Select and rename columns as follows:
            "Commission Date"               AS "Commission Date",
            "Commission Date YYYY"          AS "Commission Date YYYY",
            "Commission Date MM"            AS "Commission Date MM",
            "Revenue Recognition Date"      AS "Revenue Recognition Date",
            "Revenue Recognition Date YYYY" AS "Revenue Recognition YYYY",
            "Revenue Recognition Date MM"   AS "Revenue Recognition MM",
            "Sales Rep Name"     AS "Sales Rep",
            "Extension"          AS "Sales Actual",
            "Commission Amount"  AS "Rev Actual",
            'Novo'               AS "Product Line",
            'master_novo_sales'  AS "Data Source",
            row_hash,
            "Comm Amount tier 1",
            "Comm tier 2 diff amount"
    """
    engine = get_db_connection()
    try:
        with engine.connect() as conn:
            query = text("""
WITH commission_rates AS (
    SELECT 
        "Sales Rep Name", 
        "Commission tier 1 rate", 
        "Commission tier 2 rate"
    FROM sales_rep_commission_tier
),
commission_calculations AS (
    SELECT 
        mns."Commission Date",
        mns."Commission Date MM",
        mns."Commission Date YYYY",
        mns."Revenue Recognition Date",
        mns."Revenue Recognition Date MM",
        mns."Revenue Recognition Date YYYY",
        mns."Sales Rep Name",
        mns."Extension",
        mns."Commission Amount",
        CAST((mns."Commission Amount" * crt."Commission tier 1 rate") AS NUMERIC(15,2)) AS "Comm Amount tier 1",
        CAST((mns."Commission Amount" * crt."Commission tier 2 rate" - mns."Commission Amount" * crt."Commission tier 1 rate") AS NUMERIC(15,2)) AS "Comm tier 2 diff amount",
        mns.row_hash
    FROM master_novo_sales AS mns
    LEFT JOIN commission_rates AS crt
        ON mns."Sales Rep Name" = crt."Sales Rep Name"
)
SELECT 
    "Commission Date" AS "Commission Date",
    "Commission Date MM" AS "Commission Date MM",
    "Commission Date YYYY" AS "Commission Date YYYY",
    "Revenue Recognition Date" AS "Revenue Recognition Date",
    "Revenue Recognition Date MM" AS "Revenue Recognition MM",
    "Revenue Recognition Date YYYY" AS "Revenue Recognition YYYY",
    "Sales Rep Name" AS "Sales Rep",
    "Extension" AS "Sales Actual",
    "Commission Amount" AS "Rev Actual",
    'Novo' AS "Product Line",
    'master_novo_sales' AS "Data Source",
    row_hash,
    "Comm Amount tier 1",
    "Comm tier 2 diff amount"
FROM commission_calculations;
            """)
            result = conn.execute(query)
            df = pd.DataFrame(result.fetchall(), columns=result.keys())
            return df
    except SQLAlchemyError as e:
        print(f"❌ Error fetching and mapping Novo data: {e}")
        return None
    finally:
        engine.dispose()

def save_dataframe_to_db(df: pd.DataFrame, table_name: str = "master_novo_sales"):
    """
    Save data to the 'master_novo_sales' table by removing entries based on 'Revenue Recognition Date YYYY' and 'Revenue Recognition Date MM'.
    Return debug messages as a list.
    """
    table_name = table_name.lower()
    engine = get_db_connection()
    debug_messages = []
    
    # Generate row_hash for each row
    df["row_hash"] = df.apply(generate_row_hash, axis=1)
    
    try:
        with engine.connect() as conn:
            # Find which Revenue Recognition column names are used in this DataFrame
            rev_year_col = None
            rev_month_col = None
            
            # Check for different variations of column names
            for col in ["Revenue Recognition Date YYYY", "Revenue Recognition YYYY"]:
                if col in df.columns:
                    rev_year_col = col
                    break
            
            for col in ["Revenue Recognition Date MM", "Revenue Recognition MM"]:
                if col in df.columns:
                    rev_month_col = col
                    break
            
            if not rev_year_col or not rev_month_col:
                # Handle missing Revenue Recognition columns - fallback to Commission Date
                debug_messages.append("⚠️ Warning: Could not find Revenue Recognition Date year/month columns in the DataFrame.")
                
                # Check if there are Commission Date columns to use as fallback
                if "Commission Date YYYY" in df.columns and "Commission Date MM" in df.columns:
                    date_values = df[['Commission Date YYYY', 'Commission Date MM']].drop_duplicates().values.tolist()
                    
                    # Convert the date_values into a filterable SQL condition
                    condition = " OR ".join(
                        [f'("Commission Date YYYY" = \'{yyyy}\' AND "Commission Date MM" = \'{mm}\')' 
                         for yyyy, mm in date_values]
                    )
                    
                    debug_messages.append("⚠️ Using Commission Date columns as fallback for deletion criteria.")
                else:
                    # No date columns found - this may result in unintended behavior
                    debug_messages.append("❌ Error: No valid date columns found for deletion criteria. Operation may fail.")
                    condition = "1=0"  # Empty condition that won't delete anything
            else:
                # Use Revenue Recognition Date columns as intended
                date_values = df[[rev_year_col, rev_month_col]].drop_duplicates().values.tolist()
                
                # For database, we need to check which column names exist in the table
                inspector = inspect(engine)
                table_columns = [c["name"] for c in inspector.get_columns(table_name)]
                
                # Find the matching column names in the database table
                db_rev_year_col = None
                db_rev_month_col = None
                
                for col in table_columns:
                    # For year column
                    if col in ["Revenue Recognition Date YYYY", "Revenue Recognition YYYY"]:
                        db_rev_year_col = col
                    # For month column
                    if col in ["Revenue Recognition Date MM", "Revenue Recognition MM"]:
                        db_rev_month_col = col
                
                if not db_rev_year_col or not db_rev_month_col:
                    debug_messages.append(f"⚠️ Warning: Revenue Recognition Date columns not found in table {table_name}.")
                    # Fallback to Commission Date columns in the database
                    if "Commission Date YYYY" in table_columns and "Commission Date MM" in table_columns:
                        condition = " OR ".join(
                            [f'("Commission Date YYYY" = \'{yyyy}\' AND "Commission Date MM" = \'{mm}\')' 
                             for yyyy, mm in date_values]
                        )
                        debug_messages.append("⚠️ Using Commission Date columns in database as fallback for deletion.")
                    else:
                        debug_messages.append("❌ Error: No valid date columns found in database. Operation may fail.")
                        condition = "1=0"  # Empty condition that won't delete anything
                else:
                    # Build the condition using the actual column names from the database
                    condition = " OR ".join(
                        [f'("{db_rev_year_col}" = \'{yyyy}\' AND "{db_rev_month_col}" = \'{mm}\')' 
                         for yyyy, mm in date_values]
                    )
                    debug_messages.append(f"✅ Using Revenue Recognition Date columns for deletion criteria ({len(date_values)} date combinations).")

            # Delete existing records matching those dates
            delete_query = text(f"DELETE FROM {table_name} WHERE {condition}")
            result = conn.execute(delete_query)
            conn.commit()
            
            # Log how many records were deleted
            debug_messages.append(f"✅ Deleted {result.rowcount} records from '{table_name}' matching specified Revenue Recognition Date values.")

            # Append the dataframe to the table
            df.to_sql(table_name, con=engine, if_exists="append", index=False)
            debug_messages.append(f"✅ {len(df)} new records successfully added to '{table_name}'.")

        # If the table is 'master_novo_sales', update the harmonised table
        if table_name == "master_novo_sales":
            harmonised_messages = update_harmonised_table("master_novo_sales")
            debug_messages.extend(harmonised_messages)
            
            # Now, after updating the harmonised table, update Commission tier 2 date.
            commission_tier_2_messages = update_commission_tier_2_date()
            debug_messages.extend(commission_tier_2_messages)

    except SQLAlchemyError as e:
        error_message = str(e)
        print(f"❌ Error saving data to '{table_name}': {error_message}")
        debug_messages.append(f"❌ Error saving data to '{table_name}': {error_message}")
    finally:
        engine.dispose()

    return debug_messages

def update_harmonised_table(table_name: str):
    """
    Harmonise the specific table ('master_novo_sales') and update the harmonised_table.
    Return debug messages as a list.
    """
    debug_messages = []
    if table_name == "master_novo_sales":
        harmonised_data = map_novo_to_harmonised()
        if harmonised_data is not None:
            engine = get_db_connection()
            try:
                with engine.connect() as conn:
                    # Identify the Product Line
                    product_line = "Novo"
                    data_source = "master_novo_sales"

                    # Delete existing rows for the same product line in harmonised_table
                    delete_query = text("""DELETE FROM harmonised_table WHERE "Product Line" = :product_line AND "Data Source" = :data_source""")
                    conn.execute(delete_query, {"product_line": product_line, "data_source": data_source})
                    conn.commit()
                    print(f"✅ Deleted existing rows in 'harmonised_table' for Product Line: {product_line} with Data Source: {data_source}.")
                    debug_messages.append(f"✅ Deleted existing rows in 'harmonised_table' for Product Line: {product_line} with Data Source: {data_source}.")

                    # Append the newly harmonised data
                    harmonised_data.to_sql("harmonised_table", con=engine, if_exists="append", index=False)
                    print(f"✅ Harmonised table updated with new data for '{table_name}'.")
                    debug_messages.append(f"✅ Harmonised table updated with new data for '{table_name}'.")
                    
            except SQLAlchemyError as e:
                print(f"❌ Error updating harmonised_table: {e}")
                debug_messages.append(f"❌ Error updating harmonised_table: {e}")
            finally:
                engine.dispose()
        else:
            print(f"⚠️ No harmonised data available for '{table_name}'.")
            debug_messages.append(f"⚠️ No harmonised data available for '{table_name}'.")
            
    return debug_messages

def update_commission_tier_2_date():
    """
    For Product Line 'Novo', update the harmonised_table."Commission tier 2 date" as follows:
    
      1. For each distinct Sales Rep and year ("Commission Date YYYY"), retrieve all rows (ordered by "Commission Date MM" ascending).
      2. Look up the commission tier threshold from sales_rep_commission_tier_threshold.
      3. Compute the cumulative sum of "Sales Actual" (month by month).
      4. When the cumulative sum reaches or exceeds the tier threshold for the first time (say in month_n),
         update all rows in that group (i.e. for that Sales Rep and year) having "Commission Date MM" >= month_n with:
             "{Commission Date YYYY}-{Commission Date MM}"
         where Commission Date MM is taken from the first month where the threshold was met.
         
      Additionally, if the threshold is not reached, ensure that any old value is overwritten with NULL.
    
    Returns a list of debug messages.
    """
    debug_messages = []
    try:
        engine = get_db_connection()
        with engine.connect() as conn:
            # Step 1: Get commission tier thresholds for Product Line 'Novo'
            threshold_query = text("""
                SELECT "Sales Rep name", "Year", "Commission tier threshold"
                FROM sales_rep_commission_tier_threshold
                WHERE "Product line" = 'Novo'
            """)
            threshold_df = pd.read_sql_query(threshold_query, conn)
            # Create a dictionary keyed by (Sales Rep, Year) with the threshold value.
            threshold_dict = {
                (row["Sales Rep name"], str(row["Year"])): row["Commission tier threshold"]
                for _, row in threshold_df.iterrows()
            }
            
            # Step 2: Retrieve all rows from harmonised_table for Product Line 'Novo'
            harmonised_query = text("""
                SELECT *
                FROM harmonised_table
                WHERE "Product Line" = 'Novo'
                AND "Data Source" = 'master_novo_sales'
            """)
            harmonised_df = pd.read_sql_query(harmonised_query, conn)
            
            if harmonised_df.empty:
                debug_messages.append("No Novo rows found in harmonised_table.")
                return debug_messages
            
            # Process by grouping rows by Sales Rep and Year ("Commission Date YYYY")
            groups = harmonised_df.groupby(["Sales Rep", "Commission Date YYYY"])
            
            for (sales_rep, year), group_df in groups:
                # First, reset Commission tier 2 date to NULL for this Sales Rep and Year.
                reset_query = text("""
                    UPDATE harmonised_table
                    SET "Commission tier 2 date" = NULL
                    WHERE "Product Line" = 'Novo'
                      AND "Data Source" = 'master_novo_sales'
                      AND "Sales Rep" = :sales_rep
                      AND "Commission Date YYYY" = :year
                """)
                conn.execute(reset_query, {"sales_rep": sales_rep, "year": year})
                conn.commit()
                
                group_df = group_df.copy()
                # Convert "Commission Date MM" to integer for proper sorting
                group_df["Date_MM_int"] = group_df["Commission Date MM"].astype(int)
                group_df = group_df.sort_values("Date_MM_int")
                
                threshold_key = (sales_rep, year)
                if threshold_key not in threshold_dict:
                    debug_messages.append(f":warning: Warning - Business objective threshold missing for Sales Rep: {sales_rep}, Year: {year}. Skipping.")
                    continue
                threshold_value = threshold_dict[threshold_key]
                
                # Ensure Sales Actual is numeric and compute cumulative sum
                group_df["Sales Actual"] = pd.to_numeric(group_df["Sales Actual"], errors='coerce').fillna(0)
                group_df["cumsum"] = group_df["Sales Actual"].cumsum()
                
                # Find the first row where cumulative sales meet or exceed the threshold
                threshold_rows = group_df[group_df["cumsum"] >= threshold_value]
                if threshold_rows.empty:
                    continue
                
                # The month where the threshold is first reached
                threshold_month_int = threshold_rows.iloc[0]["Date_MM_int"]
                threshold_month = str(threshold_month_int).zfill(2)
                commission_tier_2_date = f"{year}-{threshold_month}"
                
                # Update all rows in harmonised_table for this Sales Rep and Year with "Commission Date MM" >= threshold_month
                update_query = text("""
                    UPDATE harmonised_table
                    SET "Commission tier 2 date" = :commission_tier_2_date
                    WHERE "Product Line" = 'Novo'
                      AND "Data Source" = 'master_novo_sales'
                      AND "Sales Rep" = :sales_rep
                      AND "Commission Date YYYY" = :year
                      AND "Commission Date MM" >= :threshold_month
                """)
                conn.execute(update_query, {
                    "commission_tier_2_date": commission_tier_2_date,
                    "sales_rep": sales_rep,
                    "year": year,
                    "threshold_month": threshold_month
                })
                conn.commit()
                debug_messages.append(
                    f" :exclamation: Notification - Business objective threshold reached for Sales Rep: {sales_rep}, Year: {year}, starting from month: {threshold_month}."
                )
    except Exception as e:
        debug_messages.append(f"Error updating Commission tier 2 date: {e}")
    finally:
        engine.dispose()
    return debug_messages