import pandas as pd
import xlwings as xw
import logging
from typing import Dict, Any, Optional

# ================= 日志配置 =================
# 设置日志级别为 INFO，定义输出格式：时间 - 级别 - 消息
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ================= 全局配置 =================
# JSON_CONFIG: 定义每个数据区域的处理规则。
# 结构说明：
# - Key: 区域名称（用于日志标识）
# - Value: 包含源文件、目标文件、映射关系等详细配置的字典
JSON_CONFIG: Dict[str, Dict[str, Any]] = {
    "区域1": {
        "source_file": 'C:/Users/Administrator/Desktop/test.xlsx',  # 源Excel文件路径
        "source_sheet": "Sheet1",                                   # 源工作表名称
        "target_file": 'C:/Users/Administrator/Desktop/test.xlsx',  # 目标Excel文件路径
        "target_sheet": "Sheet1",                                   # 目标工作表名称
        "target_cell": "A1",                                        # 数据写入的起始单元格
        "insert_rows_after": True,                                 # 是否在写入前插入空行（避免覆盖原有数据）
        "mapping": {                                                # 列名映射关系：{源列名: 目标列名}
            'column1': 'column2',
            'column3': 'column4',
            '区域1': '新区域1'
        }
    },
    "区域2": {
        "source_file": 'C:/Users/Administrator/Desktop/test.xlsx',
        "source_sheet": "Sheet1",
        "target_file": 'C:/Users/Administrator/Desktop/test.xlsx',
        "target_sheet": "Sheet1",
        "target_cell": "B1",
        "insert_rows_after": False,                                # 不插入行，直接覆盖或追加（取决于具体需求，此处为直接写入）
        "mapping": {
            'column1': 'column2',
            'column3': 'column4',
            '区域2': '新区域2'
        }
    },
    "区域3": {
        "source_file": 'C:/Users/Administrator/Desktop/test.xlsx',
        "source_sheet": "Sheet1",
        "target_file": 'C:/Users/Administrator/Desktop/test.xlsx',
        "target_sheet": "Sheet1",
        "target_cell": "C1",
        "insert_rows_after": False,
        "mapping": {
            'column1': 'column2',
            'column3': 'column4',
            '区域3': '新区域3'
        }
    }
}


class ExcelDataPaster:
    """
    Excel 数据粘贴器
    功能：根据配置将源Excel中的数据读取、转换列名后，写入到目标Excel指定位置。
    特性：支持多区域独立配置、数据缓存、自动插入行、异常处理。
    """

    def __init__(self, config: Dict[str, Dict[str, Any]]):
        """
        初始化粘贴器
        :param config: 数据处理配置字典
        """
        self.config = config
        self.source_cache: Dict[str, pd.DataFrame] = {}  # 缓存已读取的源数据 DataFrame，key为 "文件路径_工作表名"
        self.target_cache: Dict[str, xw.Book] = {}       # 缓存已打开的目标工作簿对象，key为文件路径
        self.app: Optional[xw.App] = None                # Excel 应用程序实例

    def start_app(self):
        """启动 Excel 应用程序实例"""
        self.app = xw.App(visible=True)
        logger.info("Excel 应用已启动")

    def get_source_data(self, source_file: str, source_sheet: str) -> Optional[pd.DataFrame]:
        """
        获取源数据（带缓存机制）
        :param source_file: 源文件路径
        :param source_sheet: 源工作表名称
        :return: DataFrame 或 None（如果读取失败）
        """
        cache_key = f"{source_file}_{source_sheet}"
        
        # 检查缓存
        if cache_key in self.source_cache:
            logger.debug(f"使用缓存数据: {source_file} - {source_sheet}")
            return self.source_cache[cache_key]

        # 读取 Excel
        try:
            df = pd.read_excel(source_file, sheet_name=source_sheet)
            self.source_cache[cache_key] = df
            logger.info(f"读取源文件: {source_file}, 工作表: {source_sheet}, 形状: {df.shape}")
            return df
        except Exception as e:
            logger.error(f"读取源文件失败 [{source_file}]: {e}")
            return None

    def get_target_workbook(self, target_file: str) -> Optional[xw.Book]:
        """
        获取目标工作簿对象（带缓存机制）
        :param target_file: 目标文件路径
        :return: xlwings Book 对象或 None（如果打开失败）
        """
        # 检查缓存
        if target_file in self.target_cache:
            return self.target_cache[target_file]

        # 打开 Excel 文件
        try:
            wb = xw.Book(target_file)
            self.target_cache[target_file] = wb
            logger.info(f"打开目标文件: {target_file}")
            return wb
        except Exception as e:
            logger.error(f"打开目标文件失败 [{target_file}]: {e}")
            return None

    def process_area(self, area_name: str, config: Dict[str, Any]) -> bool:
        """
        处理单个区域的数据迁移任务
        :param area_name: 区域名称（用于日志）
        :param config: 该区域的配置字典
        :return: 是否处理成功
        """
        logger.info(f"\n{'=' * 50}")
        logger.info(f"处理区域: {area_name}")

        # 1. 获取列映射配置
        area_mapping = config.get('mapping', {})
        if not area_mapping:
            logger.warning(f"区域 {area_name} 没有配置映射关系，跳过")
            return False

        source_columns = list(area_mapping.keys())

        # 2. 读取源数据
        df_source = self.get_source_data(config['source_file'], config['source_sheet'])
        if df_source is None:
            return False

        # 3. 验证源文件中是否存在所需的列
        missing_cols = [col for col in source_columns if col not in df_source.columns]
        if missing_cols:
            logger.warning(f"源文件中缺少列: {missing_cols}")
            logger.info(f"可用列: {list(df_source.columns)}")
            return False

        # 4. 数据预处理：提取指定列并重命名
        df_to_transfer = df_source[source_columns].copy()
        df_to_transfer.rename(columns=area_mapping, inplace=True)

        logger.info(f"列名转换映射: {area_mapping}")
        logger.info(f"待写入列: {list(df_to_transfer.columns)}")

        # 5. 获取目标工作表
        wb = self.get_target_workbook(config['target_file'])
        if wb is None:
            return False

        target_sheet_name = config['target_sheet']
        try:
            ws = wb.sheets[target_sheet_name]
        except KeyError:
            # 如果工作表不存在，则创建
            ws = wb.sheets.add(target_sheet_name)
            logger.info(f"创建工作表: {target_sheet_name}")

        # 6. 处理行插入（如果需要）
        target_cell = config['target_cell']
        rows_count = len(df_to_transfer)

        if config.get('insert_rows_after', True) and rows_count > 0:
            try:
                start_row = ws.range(target_cell).row
                # 在起始行位置批量插入空行，避免覆盖下方原有数据
                ws.range(f'{start_row+1}:{start_row + rows_count + 1}').insert('down')
                logger.info(f"在 {target_cell} 前插入了 {rows_count} 行")
            except Exception as e:
                logger.error(f"插入行失败: {e}")
                return False

        # 7. 写入数据到 Excel
        try:
            # 使用 .values 写入纯数据，不包含索引和表头
            ws.range(target_cell).options(index=False, header=False).value = df_to_transfer.values
            logger.info(f"✅ 成功将 {rows_count} 行数据写入 {target_sheet_name}!{target_cell}")
            return True
        except Exception as e:
            logger.error(f"❌ 写入数据失败: {e}")
            return False

    def run(self):
        """
        执行所有配置区域的数据处理任务
        """
        if not self.app:
            self.start_app()

        success_count = 0
        total_count = len(self.config)

        try:
            # 遍历所有配置区域
            for area_name, config in self.config.items():
                if self.process_area(area_name, config):
                    success_count += 1
        finally:
            # 无论成功与否，最后都执行清理操作
            self.cleanup()
            
        logger.info(f"\n{'=' * 50}")
        logger.info(f"处理完成: {success_count}/{total_count} 个区域成功")

    def cleanup(self):
        """
        清理资源：保存并关闭所有打开的工作簿，退出 Excel 应用
        """
        logger.info("保存所有更改...")
        for file_path, wb in self.target_cache.items():
            try:
                wb.save()
                logger.info(f"💾 保存文件: {file_path}")
            except Exception as e:
                logger.error(f"❌ 保存文件 {file_path} 失败: {e}")
            finally:
                try:
                    wb.close()
                except:
                    pass

        if self.app:
            self.app.quit()
            logger.info("Excel 应用已关闭")


def main():
    """主入口函数"""
    paster = ExcelDataPaster(JSON_CONFIG)
    paster.run()

if __name__ == '__main__':
    main()
