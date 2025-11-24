import { Diary } from '../bean/Diary';

/**
 * 日记本数据源
 */
export interface DiaryDataSource {

    /**
     * 新增一条日记本
     */
    insertDiary(diary: Diary): void;

    /**
     * 删除一条日记本
     */
    deleteDiary(diaryId: number): void;

    /**
     * 修改一条日记本
     */
    updateDiary(diaryId: number, diary: Diary): void;

    /**
     * 根据主键查询日记本
     */
    selectOne(diaryId: number): Diary | null;

    /**
     * 查找日记本列表
     */
    selectList(): Array<Diary>;

}