import { Diary } from '../bean/Diary';
import { DiaryDataSource } from '../data/DiaryDataSource';
import { DbHelper } from '../db/DbHelper';
import relationalStore from '@ohos.data.relationalStore';
import { BusinessError } from '@ohos.base';

/**
 * 日志数据源的实现类
 */
export class DiaryDataSourceImpl implements DiaryDataSource {

    private dbHelper: DbHelper;

    // 数据库列名常量
    private static readonly COLUMN_ID: string = "id";
    private static readonly COLUMN_DATE: string = "date";
    private static readonly COLUMN_WEATHER: string = "weather";
    private static readonly COLUMN_TITLE: string = "title";
    private static readonly COLUMN_CONTENT: string = "content";
    private static readonly COLUMN_CREATE_TIME: string = "create_time";
    private static readonly COLUMN_UPDATE_TIME: string = "update_time";

    // 查询列数组
    private static readonly QUERY_COLUMNS: Array<string> = [
        DiaryDataSourceImpl.COLUMN_ID,
        DiaryDataSourceImpl.COLUMN_DATE,
        DiaryDataSourceImpl.COLUMN_WEATHER,
        DiaryDataSourceImpl.COLUMN_TITLE,
        DiaryDataSourceImpl.COLUMN_CONTENT,
        DiaryDataSourceImpl.COLUMN_CREATE_TIME,
        DiaryDataSourceImpl.COLUMN_UPDATE_TIME
    ];

    constructor(dbHelper: DbHelper) {
        this.dbHelper = dbHelper;
    }

    insertDiary(diary: Diary): void {
        const rdbStore = this.dbHelper.getRdbStore();
        if (!rdbStore) {
            console.error('RdbStore is not initialized');
            return;
        }

        const valueBucket: relationalStore.ValuesBucket = {
            [DiaryDataSourceImpl.COLUMN_DATE]: diary.getDate.getTime(),
            [DiaryDataSourceImpl.COLUMN_WEATHER]: diary.getWeather,
            [DiaryDataSourceImpl.COLUMN_TITLE]: diary.getTitle,
            [DiaryDataSourceImpl.COLUMN_CONTENT]: diary.getContent,
            [DiaryDataSourceImpl.COLUMN_CREATE_TIME]: new Date().getTime(),
            [DiaryDataSourceImpl.COLUMN_UPDATE_TIME]: new Date().getTime()
        };

        rdbStore.insert(DbHelper.TABLE_DIARY, valueBucket, (err: BusinessError, rowId: number) => {
            if (err) {
                console.error(`Failed to insert diary. Code:${err.code}, message:${err.message}`);
                return;
            }
            console.info(`Succeeded in inserting diary. rowId:${rowId}`);
        });
    }

    deleteDiary(diaryId: number): void {
        const rdbStore = this.dbHelper.getRdbStore();
        if (!rdbStore) {
            console.error('RdbStore is not initialized');
            return;
        }

        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_DIARY);
        predicates.equalTo(DiaryDataSourceImpl.COLUMN_ID, diaryId);

        rdbStore.delete(predicates, (err: BusinessError, rowsDeleted: number) => {
            if (err) {
                console.error(`Failed to delete diary. Code:${err.code}, message:${err.message}`);
                return;
            }
            console.info(`Succeeded in deleting diary. rowsDeleted:${rowsDeleted}`);
        });
    }

    updateDiary(diaryId: number, diary: Diary): void {
        const rdbStore = this.dbHelper.getRdbStore();
        if (!rdbStore) {
            console.error('RdbStore is not initialized');
            return;
        }

        const valueBucket: relationalStore.ValuesBucket = {
            [DiaryDataSourceImpl.COLUMN_DATE]: diary.getDate.getTime(),
            [DiaryDataSourceImpl.COLUMN_WEATHER]: diary.getWeather,
            [DiaryDataSourceImpl.COLUMN_TITLE]: diary.getTitle,
            [DiaryDataSourceImpl.COLUMN_CONTENT]: diary.getContent,
            [DiaryDataSourceImpl.COLUMN_UPDATE_TIME]: new Date().getTime()
        };

        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_DIARY);
        predicates.equalTo(DiaryDataSourceImpl.COLUMN_ID, diaryId);

        rdbStore.update(valueBucket, predicates, (err: BusinessError, rowsUpdated: number) => {
            if (err) {
                console.error(`Failed to update diary. Code:${err.code}, message:${err.message}`);
                return;
            }
            console.info(`Succeeded in updating diary. rowsUpdated:${rowsUpdated}`);
        });
    }

    selectOne(diaryId: number): Diary | null {
        const rdbStore = this.dbHelper.getRdbStore();
        if (!rdbStore) {
            console.error('RdbStore is not initialized');
            return null;
        }

        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_DIARY);
        predicates.equalTo(DiaryDataSourceImpl.COLUMN_ID, diaryId);

        let diary: Diary | null = null;
        
        rdbStore.query(predicates, DiaryDataSourceImpl.QUERY_COLUMNS, (err: BusinessError, resultSet: relationalStore.ResultSet) => {
            if (err) {
                console.error(`Failed to query diary. Code:${err.code}, message:${err.message}`);
                return;
            }
            
            if (resultSet.goToFirstRow()) {
                diary = this.createDiary(resultSet);
            }
            resultSet.close();
        });
        
        return diary;
    }

    selectList(): Array<Diary> {
        const rdbStore = this.dbHelper.getRdbStore();
        if (!rdbStore) {
            console.error('RdbStore is not initialized');
            return [];
        }

        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_DIARY);
        predicates.orderByDesc(DiaryDataSourceImpl.COLUMN_DATE);

        const list: Array<Diary> = [];
        
        rdbStore.query(predicates, DiaryDataSourceImpl.QUERY_COLUMNS, (err: BusinessError, resultSet: relationalStore.ResultSet) => {
            if (err) {
                console.error(`Failed to query diary list. Code:${err.code}, message:${err.message}`);
                return;
            }
            
            while (resultSet.goToNextRow()) {
                const diary = this.createDiary(resultSet);
                list.push(diary);
            }
            resultSet.close();
        });
        
        return list;
    }

    private createDiary(resultSet: relationalStore.ResultSet): Diary {
        const id = resultSet.getLong(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_ID));
        const date = resultSet.getLong(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_DATE));
        const weather = resultSet.getString(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_WEATHER));
        const title = resultSet.getString(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_TITLE));
        const content = resultSet.getString(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_CONTENT));
        const createTime = resultSet.getLong(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_CREATE_TIME));
        const updateTime = resultSet.getLong(resultSet.getColumnIndex(DiaryDataSourceImpl.COLUMN_UPDATE_TIME));

        const diary = new Diary();
        diary.setId = id;
        diary.setDate = new Date(date);
        diary.setWeather = weather;
        diary.setTitle = title;
        diary.setContent = content;
        diary.setCreateTime = new Date(createTime);
        diary.setUpdateTime = new Date(updateTime);
        
        return diary;
    }

}