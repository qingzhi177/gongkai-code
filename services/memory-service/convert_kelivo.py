import json
import sys
import httpx

def convert_kelivo(input_file, output_file=None):
    """将 Kelivo 导出的 chats.json 转换为记忆库可导入的格式"""
    with open(input_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    conversations = data.get('conversations', [])
    all_messages = data.get('messages', [])
    
    # 建立消息索引
    msg_by_id = {msg['id']: msg for msg in all_messages}
    # 按 groupId 分组
    group_versions = {}
    for msg in all_messages:
        gid = msg.get('groupId') or msg['id']
        if gid not in group_versions:
            group_versions[gid] = []
        group_versions[gid].append(msg)
    
    results = []
    for conv in conversations:
        conv_id = conv['id']
        title = conv.get('title', '')
        version_selections = conv.get('versionSelections', {})
        message_ids = conv.get('messageIds', [])
        
        # 按 messageIds 顺序处理，每个位置只取一条
        conv_messages = []
        processed_positions = set()
        
        for mid in message_ids:
            msg = msg_by_id.get(mid)
            if not msg:
                continue
            gid = msg.get('groupId') or msg['id']
            
            # 同一个 groupId 表示同一个位置的多个版本
            # 如果这个位置已经处理过，跳过
            if gid in processed_positions:
                continue
            processed_positions.add(gid)
            
            # 获取这个位置的所有版本
            versions = group_versions.get(gid, [msg])
            # 选择正确的版本
            if gid in version_selections:
                selected_ver = version_selections[gid]
                chosen = None
                for v in versions:
                    if v.get('version', 0) == selected_ver:
                        chosen = v
                        break
                if not chosen:
                    chosen = versions[0]
            else:
                # 没有版本选择记录，取 version=0 或第一条
                chosen = next((v for v in versions if v.get('version', 0) == 0), versions[0])
            
            content = chosen.get('content', '')
            if not content:
                continue
            
            conv_messages.append({
                "role": chosen['role'],
                "content": content,
                "timestamp": chosen.get('timestamp', '')
            })
        
        if conv_messages:
            results.append({
                "conv_id": conv_id,
                "title": title,
                "created_at": conv.get('createdAt', ''),
                "messages": conv_messages
            })
    
    # 输出
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"转换完成！{len(results)} 个对话")
    for r in results:
        print(f"  - {r['title']}: {len(r['messages'])} 条消息")
    
    return results

def import_to_memory(results, api_url="http://localhost:8001"):
    """直接导入到记忆服务"""
    for conv in results:
        messages = [{"role": m["role"], "content": m["content"], "timestamp": m.get("timestamp", "")} for m in conv["messages"]]
        response = httpx.post(
            f"{api_url}/import_conversation",
            json={
                "client": "kelivo",
                "conv_date": conv.get("created_at", ""),
                "messages": messages
            },
            timeout=120.0
        )
        result = response.json()
        if result.get("status") == "ok":
            print(f"  导入成功: {conv['title']} ({result['saved']} 条)")
        else:
            print(f"  导入失败: {conv['title']} - {result.get('detail', '')}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  转换并预览: python convert_kelivo.py input.json")
        print("  转换并直接导入: python convert_kelivo.py input.json --import")
        sys.exit(1)
    input_file = sys.argv[1]
    if len(sys.argv) > 2 and sys.argv[2] == '--import':
        print(f"正在转换 {input_file} ...")
        results = convert_kelivo(input_file)
        print(f"\n开始导入到记忆库...")
        import_to_memory(results)
    else:
        output_file = input_file.replace('.json', '_converted.json')
        convert_kelivo(input_file, output_file)
        print(f"\n已保存到: {output_file}")
