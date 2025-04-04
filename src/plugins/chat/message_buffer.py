from ..person_info.person_info import person_info_manager
from src.common.logger import get_module_logger
import asyncio
from dataclasses import dataclass
from .message import MessageRecv
from ..message.message_base import BaseMessageInfo
import hashlib
from typing import Dict
from dataclasses import dataclass, field
from collections import OrderedDict
import random
import time

logger = get_module_logger("message_buffer")

@dataclass
class CacheMessages:
    message: MessageRecv 
    cache_determination: asyncio.Event = field(default_factory=asyncio.Event)  # 判断缓冲是否产生结果
    result: str = "U"


class MassageBuffer:
    def __init__(self):
        self.buffer_pool: Dict[str, OrderedDict[str, CacheMessages]] = {}
        self.lock = asyncio.Lock()

    def get_person_id_(self, platform:str, user_id:str, group_id:str):
        """获取唯一id"""
        group_id = group_id or "私聊"
        key = f"{platform}_{user_id}_{group_id}"
        return hashlib.md5(key.encode()).hexdigest()

    async def start_caching_messages(self, message:MessageRecv):
        """添加消息，启动缓冲"""
        person_id_ = self.get_person_id_(message.message_info.platform,
                                             message.message_info.user_info.user_id,
                                             message.message_info.group_info.group_id)

        async with self.lock:
            if person_id_ not in self.buffer_pool:
                self.buffer_pool[person_id_] = OrderedDict()

            # 查找最近的处理成功消息(T)
            recent_F_count = 0
            for msg_id in reversed(self.buffer_pool[person_id_]):
                msg = self.buffer_pool[person_id_][msg_id]
                if msg.result == "T":
                    break
                elif msg.result == "F":
                    recent_F_count += 1
            
            # 判断条件：最近T之后有超过3-5条F
            if (recent_F_count >= random.randint(3, 5)):
                new_msg = CacheMessages(message=message, result="T")
                new_msg.cache_determination.set()
                self.buffer_pool[person_id_][message.message_info.message_id] = new_msg
                logger.debug(f"快速处理消息(已堆积{recent_F_count}条F): {message.message_info.message_id}")
                return

            # 标记该用户之前的未处理消息
            for msg_id, cache_msg in self.buffer_pool[person_id_].items():
                if cache_msg.result == "U":
                    cache_msg.result = "F"
                    cache_msg.cache_determination.set()
                    logger.debug(f"被新消息覆盖信息id: {cache_msg.message.message_info.message_id}")
            
            # 添加新消息
            self.buffer_pool[person_id_][message.message_info.message_id] = CacheMessages(message=message)
        
        # 启动3秒缓冲计时器
        person_id = person_info_manager.get_person_id(message.message_info.user_info.platform,
                                                      message.message_info.user_info.user_id)
        asyncio.create_task(self.save_message_interval(person_id, message.message_info))
        asyncio.create_task(self._debounce_processor(person_id_,
                                                     message.message_info.message_id,
                                                     person_id))

    async def _debounce_processor(self, person_id_: str, message_id: str, person_id: str):
        """等待3秒无新消息"""
        interval_time = await person_info_manager.get_value(person_id, "msg_interval")
        if not isinstance(interval_time, (int, str)) or not str(interval_time).isdigit():
            logger.debug("debounce_processor无效的时间")
            return
        interval_time = max(0.5, int(interval_time) / 1000)
        await asyncio.sleep(interval_time)
        
        async with self.lock:
            if (person_id_ not in self.buffer_pool or 
                message_id not in self.buffer_pool[person_id_]):
                logger.debug(f"消息异常被清理，msgid: {message_id}")
                return
            
            cache_msg = self.buffer_pool[person_id_][message_id]
            if cache_msg.result == "U":
                cache_msg.result = "T"
                cache_msg.cache_determination.set()


    async def query_buffer_result(self, message:MessageRecv) -> bool:
        """查询缓冲结果，并清理"""
        person_id_ = self.get_person_id_(message.message_info.platform,
                                         message.message_info.user_info.user_id,
                                         message.message_info.group_info.group_id)
                                        
        
        async with self.lock:
            user_msgs = self.buffer_pool.get(person_id_, {})
            cache_msg = user_msgs.get(message.message_info.message_id)
            
            if not cache_msg:
                logger.debug(f"查询异常，消息不存在，msgid: {message.message_info.message_id}")
                return False  # 消息不存在或已清理
            
        try:
            await asyncio.wait_for(cache_msg.cache_determination.wait(), timeout=10)
            result = cache_msg.result == "T"

            if result:
                async with self.lock:  # 再次加锁
                    # 清理所有早于当前消息的已处理消息， 收集所有早于当前消息的F消息的processed_plain_text
                    keep_msgs = OrderedDict()
                    combined_text = []
                    found = False
                    is_text = False
                    for msg_id, msg in self.buffer_pool[person_id_].items():
                        if msg_id == message.message_info.message_id:
                            found = True
                            is_text = msg.message.message_segment.type == "text"
                            combined_text.append(msg.message.processed_plain_text)
                            continue
                        if found:
                            keep_msgs[msg_id] = msg
                        elif msg.result == "F":
                            # 收集F消息的文本内容
                            if (hasattr(msg.message, 'processed_plain_text') 
                                and msg.message.message_segment.type == "text"
                                and msg.message.processed_plain_text):
                                combined_text.append(msg.message.processed_plain_text)
                        elif msg.result == "U":
                            logger.debug(f"异常未处理信息id： {msg.message.message_info.message_id}")

                    # 更新当前消息的processed_plain_text
                    if combined_text and combined_text[0] != message.processed_plain_text and is_text:
                        message.processed_plain_text = "".join(combined_text)
                        logger.debug(f"整合了{len(combined_text)-1}条F消息的内容到当前消息")

                    self.buffer_pool[person_id_] = keep_msgs
            return result
        except asyncio.TimeoutError:
            logger.debug(f"查询超时消息id： {message.message_info.message_id}")
            return False
    
    async def save_message_interval(self, person_id:str, message:BaseMessageInfo):
        message_interval_list = await person_info_manager.get_value(person_id, "msg_interval_list")
        now_time_ms = int(round(time.time() * 1000))
        if len(message_interval_list) < 1000:
            message_interval_list.append(now_time_ms)
        else:
            message_interval_list = message_interval_list.pop(0)
            message_interval_list.append(now_time_ms)
        data = {
            "platform" : message.platform,
            "user_id" : message.user_info.user_id,
            "nickname" : message.user_info.user_nickname,
            "konw_time" : int(time.time())
        }
        await person_info_manager.update_one_field(person_id, "msg_interval_list", message_interval_list, data)


message_buffer = MassageBuffer()