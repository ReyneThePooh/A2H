import { Diary } from '../bean/Diary';
import { Mapp } from '../Mapp';
import { SizeUtils } from '../utils/SizeUtils';
import { TimeUtils } from '../utils/TimeUtils';
import { DiaryBrowseActivity } from './DiaryBrowseActivity';
import { BaseActivity } from './BaseActivity';
import { collections } from '@kit.ArkTS';
import { BusinessError } from '@kit.BasicServicesKit';
import { Intent } from '@kit.AbilityKit';
import { CommonConstants } from '@kit.ArkUI';
import { ListItem, List } from '@kit.ArkUI';
import { router } from '@kit.ArkUI';

/**
 * 日志列表
 */
@Entry
@Component
struct DiaryListActivity {

  private static readonly REQUEST_BROWSE: number = 1;

  @State diaryList: Array<Diary> = []; // 日记列表数据
  private diaryRecyclerAdapter: DiaryRecyclerAdapter = new DiaryRecyclerAdapter();

  aboutToAppear(): void {
    // 初始化数据
    this.loadDiaryList();
  }

  /**
   * 页面显示时的生命周期方法
   */
  onPageShow(): void {
    // 页面显示时检查是否需要刷新数据
    // 这里可以处理从其他页面返回时的数据刷新逻辑
    // 模拟onActivityResult的功能
    const params = router.getParams() as Record<string, Object>;
    if (params && params['refresh'] === true) {
      // 如果从其他页面返回需要刷新数据
      this.loadDiaryList();
    }
  }

  /**
   * 页面隐藏时的生命周期方法
   */
  onPageHide(): void {
    // 页面隐藏时的清理工作
  }

  /**
   * 页面返回时的生命周期方法
   */
  onBackPress(): boolean {
    // 返回上一页
    router.back();
    return true; // 返回true表示已处理返回事件
  }

  /**
   * 加载日记列表数据
   */
  private loadDiaryList(): void {
    const mappInstance = Mapp.getInstance();
    if (mappInstance) {
      const list = mappInstance.getDiaryDataSource().selectList();
      this.diaryList = list;
      this.diaryRecyclerAdapter.setNewData(list);
    }
  }

  /**
   * 页面返回数据处理
   */
  private onActivityResult(requestCode: number, resultCode: number, data: Intent | null): void {
    if (requestCode === DiaryListActivity.REQUEST_BROWSE && resultCode === CommonConstants.RESULT_OK) {
      // 刷新数据
      this.loadDiaryList();
    }
  }

  /**
   * 构建意图
   */
  static buildIntent(context: object): Intent {
    const intent = new Intent();
    // TODO: 设置意图参数
    return intent;
  }

  /**
   * 处理列表项点击事件
   */
  private onItemClick(diary: Diary, position: number): void {
    // 跳转到日记浏览页面
    router.pushUrl({
      url: 'pages/DiaryBrowseActivity',
      params: { 
        diaryId: diary.getId(),
        requestCode: DiaryListActivity.REQUEST_BROWSE 
      }
    }).then(() => {
      console.info('跳转到日记浏览页面成功');
    }).catch((err: BusinessError) => {
      console.error('跳转到日记浏览页面失败: ' + JSON.stringify(err));
    });
  }

  /**
   * 获取列表项间隔距离
   */
  private getItemOffsets(index: number, count: number): number {
    if (index < count - 1) {
      return SizeUtils.dp2px(30);
    }
    return 0;
  }

  build() {
    Column() {
      // 标题栏
      Column() {
        Row() {
          Text("日记列表")
            .fontSize(20)
            .fontWeight(FontWeight.Bold)
            .fontColor(/* TODO_RESOURCE: R.color.white */)
          
          // 返回按钮
          Image(/* TODO_RESOURCE: R.drawable.ic_back */)
            .width(24)
            .height(24)
            .onClick(() => {
              this.onBackPress();
            })
        }
        .width('100%')
        .height(56)
        .padding({ left: 16, right: 16 })
        .justifyContent(FlexAlign.SpaceBetween)
        .alignItems(VerticalAlign.Center)
      }
      .width('100%')
      .backgroundColor(/* TODO_RESOURCE: R.color.colorPrimary */)

      // 日记列表
      List({ space: 0 }) {
        ForEach(this.diaryList, (diary: Diary, index: number) => {
          ListItem() {
            this.DiaryItemBuilder(diary, index)
          }
        }, (diary: Diary) => diary.getId().toString())
      }
      .width('100%')
      .layoutWeight(1)
      .padding(30)
      .backgroundColor(/* TODO_RESOURCE: R.color.pink_200 */)
      .onScrollIndex((start: number, end: number) => {
        // 列表滚动事件处理
        console.info(`列表滚动: ${start} - ${end}`);
      })
    }
    .width('100%')
    .height('100%')
    .backgroundColor(/* TODO_RESOURCE: R.color.pink_200 */)
  }

  /**
   * 日记项构建器
   */
  @Builder
  DiaryItemBuilder(diary: Diary, index: number) {
    Column() {
      // 标题
      Text(diary.getTitle())
        .width('100%')
        .textAlign(TextAlign.Start)
        .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
        .fontSize(16)

      // 日期和天气行
      Row() {
        Text(this.formatDate(diary.getDate()))
          .layoutWeight(1)
          .textAlign(TextAlign.Start)
          .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
          .fontSize(14)

        Text(diary.getWeather())
          .margin({ left: 10 })
          .fontColor(/* TODO_RESOURCE: R.color.pink_500 */)
          .fontSize(14)
      }
      .width('100%')
      .margin({ top: 10 })

      // 字数和修改时间行
      Row() {
        Text(`字数${diary.getContent().length}个`)
          .textAlign(TextAlign.Start)
          .fontColor(/* TODO_RESOURCE: R.color.brown */)
          .fontSize(12)

        Text(`编辑于${TimeUtils.getSimpleTime(diary.getUpdateTime().getTime())}`)
          .layoutWeight(1)
          .margin({ left: 10 })
          .textAlign(TextAlign.End)
          .fontColor(/* TODO_RESOURCE: R.color.brown */)
          .fontSize(12)
      }
      .width('100%')
      .margin({ top: 10 })
    }
    .width('100%')
    .padding({ 
      left: 15, 
      top: 15, 
      right: 15, 
      bottom: 10 
    })
    .backgroundColor(/* TODO_RESOURCE: @drawable/btn_diary */)
    .onClick(() => {
      this.onItemClick(diary, index);
    })
    .margin({ bottom: this.getItemOffsets(index, this.diaryList.length) })
  }

  /**
   * 格式化日期
   */
  private formatDate(date: Date): string {
    const year = date.getFullYear();
    const month = date.getMonth() + 1;
    const day = date.getDate();
    return `${year}年${month}月${day}日`;
  }

}

@Component
export struct DiaryRecyclerAdapter {

  @State private list: Array<Diary> = []; // 日记本列表数据
  private onItemClickListener?: OnItemClickListener; // 点击事件

  build() {
    // 适配器UI将在后续步骤中实现
  }

  /**
   * 设置新数据
   */
  setNewData(list: Array<Diary> | null): void {
    if (list) {
      this.list = list;
    } else {
      this.list = [];
    }
    // TODO: 通知数据变化
  }

  /**
   * 设置单个日记的点击事件
   */
  setOnItemClickListener(listener: OnItemClickListener | null): void {
    this.onItemClickListener = listener || undefined;
  }

  /**
   * 获取数据项数量
   */
  getItemCount(): number {
    return this.list.length;
  }

  /**
   * 获取指定位置的数据项
   */
  getItem(position: number): Diary | null {
    if (position >= 0 && position < this.list.length) {
      return this.list[position];
    }
    return null;
  }

  /**
   * 绑定数据到视图
   */
  onBindViewHolder(diary: Diary, position: number): void {
    // 数据绑定逻辑将在后续步骤中实现
    if (this.onItemClickListener) {
      // 点击事件绑定
    }
  }

  /**
   * 处理适配器项点击事件
   */
  private handleItemClick(diary: Diary, position: number): void {
    if (this.onItemClickListener) {
      this.onItemClickListener.onItemClick(diary, position);
    }
  }

}

/**
 * 日记的点击事件
 */
interface OnItemClickListener {

  onItemClick(diary: Diary, position: number): void;

}