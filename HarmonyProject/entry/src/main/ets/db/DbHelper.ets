import relationalStore from '@ohos.data.relationalStore';
import { BusinessError } from '@ohos.base';
import common from '@ohos.app.ability.common';

/**
 * 数据库操作工具
 */
export class DbHelper {
    public static readonly DB_NAME: string = "diary.db"; // 数据库的名称
    public static readonly DB_VERSION: number = 1; // 数据库的版本号
    public static readonly TABLE_DIARY: string = "diary"; // 日记表
    
    private rdbStore: relationalStore.RdbStore | null = null;
    
    constructor(context: common.Context) {
        this.initRdbStore(context);
    }
    
    /**
     * 初始化RDB数据库
     */
    private initRdbStore(context: common.Context): void {
        const STORE_CONFIG: relationalStore.StoreConfig = {
            name: DbHelper.DB_NAME,
            securityLevel: relationalStore.SecurityLevel.S1
        };
        
        relationalStore.getRdbStore(context, STORE_CONFIG, (err: BusinessError, store: relationalStore.RdbStore) => {
            if (err) {
                console.error(`Failed to get RdbStore. Code:${err.code}, message:${err.message}`);
                return;
            }
            console.info('Succeeded in getting RdbStore.');
            this.rdbStore = store;
            this.createDiaryTable();
        });
    }
    
    /**
     * 创建日记表
     */
    private createDiaryTable(): void {
        if (!this.rdbStore) {
            console.error('RdbStore is not initialized');
            return;
        }
        
        const sql = `CREATE TABLE IF NOT EXISTS ${DbHelper.TABLE_DIARY} (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            date INTEGER NOT NULL,
            weather TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            create_time INTEGER NOT NULL,
            update_time INTEGER NOT NULL
        )`;
        
        this.rdbStore.executeSql(sql, [], (err: BusinessError) => {
            if (err) {
                console.error(`Failed to create diary table. Code:${err.code}, message:${err.message}`);
                return;
            }
            console.info('Succeeded in creating diary table.');
        });
    }
    
    /**
     * 获取数据库实例
     */
    public getRdbStore(): relationalStore.RdbStore | null {
        return this.rdbStore;
    }
    
}