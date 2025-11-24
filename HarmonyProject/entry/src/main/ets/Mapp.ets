import { DiaryDataSource } from './data/DiaryDataSource';
import { DiaryDataSourceImpl } from './data/impl/DiaryDataSourceImpl';
import { DbHelper } from './db/DbHelper';
import UIAbility from '@ohos.app.ability.UIAbility';

/**
 * 应用主类
 */
export class Mapp extends UIAbility {

    private static instance: Mapp | null = null; // 单例

    private dbHelper: DbHelper | null = null; // 数据库操作工具
    private diaryDataSource: DiaryDataSource | null = null; // 日记数据源

    onCreate(): void {
        super.onCreate();
        Mapp.instance = this;
    }

    /**
     * 获取实例
     */
    public static getInstance(): Mapp | null {
        return Mapp.instance;
    }

    /**
     * 懒加载获取数据库操作工具
     */
    public getDbHelper(): DbHelper {
        if (this.dbHelper == null) {
            this.dbHelper = new DbHelper(this.context);
        }
        return this.dbHelper;
    }

    /**
     * 懒加载获取日记数据源
     */
    public getDiaryDataSource(): DiaryDataSource {
        if (this.diaryDataSource == null) {
            this.diaryDataSource = new DiaryDataSourceImpl(this.getDbHelper());
        }
        return this.diaryDataSource;
    }

}