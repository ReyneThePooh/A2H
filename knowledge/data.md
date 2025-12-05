# ArkTS 数据库操作指南

## 目录

- [数据库初始化](#数据库初始化)
- [数据模型定义](#数据模型定义)
- [数据库基本操作](#数据库基本操作)
  - [创建数据表](#创建数据表)
  - [插入数据](#插入数据)
  - [删除数据](#删除数据)
  - [更新数据](#更新数据)
  - [查询数据](#查询数据)
- [完整示例代码](#完整示例代码)

## 数据库初始化

### 1. 引入依赖模块

```typescript
import relationalStore from '@ohos.data.relationalStore';
import { BusinessError } from '@ohos.base';
import common from '@ohos.app.ability.common';
```

### 2. 创建数据库工具类

```typescript
export class DbHelper {
    public static readonly DB_NAME: string = "app.db";
    public static readonly DB_VERSION: number = 1;
    public static readonly TABLE_NAME: string = "data_table";
    
    private rdbStore: relationalStore.RdbStore | null = null;
    private initPromise: Promise<void> | null = null;
    
    constructor(context: common.Context) {
        this.initPromise = this.initRdbStore(context);
    }
    
    // 获取数据库实例
    public getRdbStore(): relationalStore.RdbStore | null {
        return this.rdbStore;
    }
    
    // 等待初始化完成
    public async waitForInit(): Promise<void> {
        if (this.initPromise) {
            await this.initPromise;
        }
    }
    
    // 检查是否已初始化
    public isInitialized(): boolean {
        return this.rdbStore !== null;
    }
}
```

### 3. 初始化数据库连接

```typescript
private async initRdbStore(context: common.Context): Promise<void> {
    const STORE_CONFIG: relationalStore.StoreConfig = {
        name: DbHelper.DB_NAME,
        securityLevel: relationalStore.SecurityLevel.S1
    };
    
    try {
        // 获取数据库实例
        this.rdbStore = await relationalStore.getRdbStore(context, STORE_CONFIG);
        console.info('数据库实例获取成功');
        
        // 创建数据表
        await this.createTable();
        console.info('数据库初始化完成');
    } catch (err) {
        const error = err as BusinessError;
        console.error(`数据库初始化失败. Code:${error.code}, message:${error.message}`);
    }
}
```

## 创建数据表

### 定义表结构

```typescript
private async createTable(): Promise<void> {
    if (!this.rdbStore) {
        throw new Error('RdbStore 未初始化');
    }
    
    // 定义SQL建表语句
    const sql = `CREATE TABLE IF NOT EXISTS ${DbHelper.TABLE_NAME} (
        id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
        field1 TEXT NOT NULL,
        field2 INTEGER NOT NULL,
        field3 REAL,
        create_time INTEGER NOT NULL,
        update_time INTEGER NOT NULL
    )`;
    
    try {
        await this.rdbStore.executeSql(sql);
        console.info('数据表创建成功');
    } catch (err) {
        const error = err as BusinessError;
        console.error(`创建数据表失败. Code:${error.code}, message:${error.message}`);
    }
}
```

## 数据库基本操作

### 插入数据

```typescript
async insertData(data: YourModel): Promise<number> {
    await this.dbHelper.waitForInit();
    
    const rdbStore = this.dbHelper.getRdbStore();
    if (!rdbStore) {
        throw new Error('RdbStore 未初始化');
    }
    
    // 准备数据
    const valueBucket: relationalStore.ValuesBucket = {
        "field1": data.field1,
        "field2": data.field2,
        "field3": data.field3,
        "create_time": new Date().getTime(),
        "update_time": new Date().getTime()
    };
    
    // 执行插入
    const rowId = await rdbStore.insert(DbHelper.TABLE_NAME, valueBucket);
    console.info(`插入成功, rowId: ${rowId}`);
    return rowId;
}
```

### 删除数据

```typescript
async deleteData(id: number): Promise<void> {
    await this.dbHelper.waitForInit();
    
    const rdbStore = this.dbHelper.getRdbStore();
    if (!rdbStore) {
        throw new Error('RdbStore 未初始化');
    }
    
    // 构建查询条件
    const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
    predicates.equalTo("id", id);
    
    try {
        const rowsDeleted = await rdbStore.delete(predicates);
        console.info(`删除成功, 影响行数: ${rowsDeleted}`);
    } catch (err) {
        const error = err as BusinessError;
        console.error(`删除失败. Code:${error.code}, message:${error.message}`);
    }
}
```

### 更新数据

```typescript
async updateData(id: number, data: YourModel): Promise<void> {
    await this.dbHelper.waitForInit();
    
    const rdbStore = this.dbHelper.getRdbStore();
    if (!rdbStore) {
        throw new Error('RdbStore 未初始化');
    }
    
    // 准备更新数据
    const valueBucket: relationalStore.ValuesBucket = {
        "field1": data.field1,
        "field2": data.field2,
        "field3": data.field3,
        "update_time": new Date().getTime()
    };
    
    // 构建查询条件
    const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
    predicates.equalTo("id", id);
    
    try {
        const rowsUpdated = await rdbStore.update(valueBucket, predicates);
        console.info(`更新成功, 影响行数: ${rowsUpdated}`);
    } catch (err) {
        const error = err as BusinessError;
        console.error(`更新失败. Code:${error.code}, message:${error.message}`);
    }
}
```

### 查询数据

#### 单条查询

```typescript
async selectOne(id: number): Promise<YourModel | null> {
    await this.dbHelper.waitForInit();
    
    const rdbStore = this.dbHelper.getRdbStore();
    if (!rdbStore) {
        return null;
    }
    
    const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
    predicates.equalTo("id", id);
    
    try {
        const resultSet = await rdbStore.query(predicates, ["id", "field1", "field2", "field3", "create_time", "update_time"]);
        
        let result: YourModel | null = null;
        if (resultSet.goToFirstRow()) {
            result = this.createModel(resultSet);
        }
        
        resultSet.close();
        return result;
    } catch (err) {
        const error = err as BusinessError;
        console.error(`查询失败. Code:${error.code}, message:${error.message}`);
        return null;
    }
}
```

#### 列表查询

```typescript
async selectList(): Promise<Array<YourModel>> {
    await this.dbHelper.waitForInit();
    
    const rdbStore = this.dbHelper.getRdbStore();
    if (!rdbStore) {
        return [];
    }
    
    const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
    predicates.orderByDesc("create_time"); // 按创建时间降序排序
    
    const list: Array<YourModel> = [];
    
    try {
        const resultSet = await rdbStore.query(predicates, ["id", "field1", "field2", "field3", "create_time", "update_time"]);
        
        while (resultSet.goToNextRow()) {
            const item = this.createModel(resultSet);
            list.push(item);
        }
        
        resultSet.close();
        console.info(`列表查询成功, 共 ${list.length} 条`);
    } catch (err) {
        const error = err as BusinessError;
        console.error(`列表查询失败. Code:${error.code}, message:${error.message}`);
    }
    
    return list;
}
```

### 结果集处理

```typescript
private createModel(resultSet: relationalStore.ResultSet): YourModel {
    const id = resultSet.getLong(resultSet.getColumnIndex("id"));
    const field1 = resultSet.getString(resultSet.getColumnIndex("field1"));
    const field2 = resultSet.getLong(resultSet.getColumnIndex("field2"));
    const field3 = resultSet.getDouble(resultSet.getColumnIndex("field3"));
    const createTime = resultSet.getLong(resultSet.getColumnIndex("create_time"));
    const updateTime = resultSet.getLong(resultSet.getColumnIndex("update_time"));
    
    const model = new YourModel();
    model.id = id;
    model.field1 = field1;
    model.field2 = field2;
    model.field3 = field3;
    model.createTime = new Date(createTime);
    model.updateTime = new Date(updateTime);
    
    return model;
}
```

## 完整示例代码

```typescript
import relationalStore from '@ohos.data.relationalStore';
import { BusinessError } from '@ohos.base';
import common from '@ohos.app.ability.common';

/**
 * 数据库操作工具类
 */
export class DbHelper {
    public static readonly DB_NAME: string = "app.db";
    public static readonly TABLE_NAME: string = "data_table";
    
    private rdbStore: relationalStore.RdbStore | null = null;
    private initPromise: Promise<void> | null = null;
    
    constructor(context: common.Context) {
        this.initPromise = this.initRdbStore(context);
    }
    
    private async initRdbStore(context: common.Context): Promise<void> {
        const STORE_CONFIG: relationalStore.StoreConfig = {
            name: DbHelper.DB_NAME,
            securityLevel: relationalStore.SecurityLevel.S1
        };
        
        try {
            this.rdbStore = await relationalStore.getRdbStore(context, STORE_CONFIG);
            await this.createTable();
        } catch (err) {
            const error = err as BusinessError;
            console.error(`数据库初始化失败. Code:${error.code}, message:${error.message}`);
        }
    }
    
    private async createTable(): Promise<void> {
        if (!this.rdbStore) {
            throw new Error('RdbStore 未初始化');
        }
        
        const sql = `CREATE TABLE IF NOT EXISTS ${DbHelper.TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            field1 TEXT NOT NULL,
            field2 INTEGER NOT NULL,
            field3 REAL,
            create_time INTEGER NOT NULL,
            update_time INTEGER NOT NULL
        )`;
        
        await this.rdbStore.executeSql(sql);
    }
    
    public async waitForInit(): Promise<void> {
        if (this.initPromise) {
            await this.initPromise;
        }
    }
    
    public getRdbStore(): relationalStore.RdbStore | null {
        return this.rdbStore;
    }
}

/**
 * 数据操作类
 */
export class DataRepository {
    private dbHelper: DbHelper;
    
    constructor(dbHelper: DbHelper) {
        this.dbHelper = dbHelper;
    }
    
    async insert(data: YourModel): Promise<number> {
        await this.dbHelper.waitForInit();
        const rdbStore = this.dbHelper.getRdbStore();
        
        const valueBucket: relationalStore.ValuesBucket = {
            "field1": data.field1,
            "field2": data.field2,
            "create_time": new Date().getTime(),
            "update_time": new Date().getTime()
        };
        
        return await rdbStore.insert(DbHelper.TABLE_NAME, valueBucket);
    }
    
    async delete(id: number): Promise<void> {
        await this.dbHelper.waitForInit();
        const rdbStore = this.dbHelper.getRdbStore();
        
        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
        predicates.equalTo("id", id);
        
        await rdbStore.delete(predicates);
    }
    
    async update(id: number, data: YourModel): Promise<void> {
        await this.dbHelper.waitForInit();
        const rdbStore = this.dbHelper.getRdbStore();
        
        const valueBucket: relationalStore.ValuesBucket = {
            "field1": data.field1,
            "field2": data.field2,
            "update_time": new Date().getTime()
        };
        
        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
        predicates.equalTo("id", id);
        
        await rdbStore.update(valueBucket, predicates);
    }
    
    async queryAll(): Promise<Array<YourModel>> {
        await this.dbHelper.waitForInit();
        const rdbStore = this.dbHelper.getRdbStore();
        
        const predicates: relationalStore.RdbPredicates = new relationalStore.RdbPredicates(DbHelper.TABLE_NAME);
        const resultSet = await rdbStore.query(predicates, ["id", "field1", "field2", "create_time"]);
        
        const list: Array<YourModel> = [];
        while (resultSet.goToNextRow()) {
            // 处理结果集
        }
        resultSet.close();
        
        return list;
    }
}
```

## 注意事项

1. **错误处理**：所有数据库操作都应使用 try-catch 处理异常
2. **资源释放**：查询完成后必须调用 `resultSet.close()` 释放资源
3. **异步操作**：数据库操作都是异步的，需要使用 async/await
4. **线程安全**：确保在正确的线程中访问数据库
5. **数据备份**：重要数据需要定期备份到应用沙箱目录