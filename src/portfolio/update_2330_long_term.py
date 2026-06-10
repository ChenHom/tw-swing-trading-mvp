import sqlite3
from src.portfolio.db import init_db, get_db_connection
from src.portfolio.projection import PortfolioProjection

def main():
    db_path = "data/app.db"
    
    # 1. Run init_db to ensure migration runs and adds the columns if they don't exist yet
    print("確保資料庫結構已升級...")
    init_db(db_path)
    
    conn = get_db_connection(db_path)
    cursor = conn.cursor()
    
    # 2. Update the existing fills of 2330 for simulation-main to have is_long_term = 1
    print("更新 2330 的成交事實 (fills) 為長期持有...")
    cursor.execute(
        """
        UPDATE fills
        SET is_long_term = 1
        WHERE symbol = '2330' AND side = 'BUY' AND account_id = 'simulation-main'
        """
    )
    updated_fills = cursor.rowcount
    print(f"已更新 {updated_fills} 筆 2330 的 BUY 成交紀錄。")
    
    conn.commit()
    
    # 3. Rebuild projections from ledger to populate position_lots correctly
    print("從交易事實重建持倉投影 (Rebuilding projections)...")
    projection = PortfolioProjection(conn)
    projection.rebuild_from_ledger("simulation-main")
    
    # 4. Verify position lots
    cursor.execute(
        """
        SELECT lot_id, symbol, quantity, price, is_long_term
        FROM position_lots
        WHERE account_id = 'simulation-main' AND symbol = '2330'
        """
    )
    lots = cursor.fetchall()
    print("更新後的 2330 持倉部位：")
    for lot in lots:
        print(f"  - Lot ID: {lot['lot_id']}, 股數: {lot['quantity']}, 價格: {lot['price'] / 10000.0:.2f}, 長期持有: {bool(lot['is_long_term'])}")
        
    conn.close()
    print("更新完成！")

if __name__ == "__main__":
    main()
