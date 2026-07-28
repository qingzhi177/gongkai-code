非流式模式下工具调用不透传给前端。

流式模式功能3已经做了工具透传，但非流式还是只返回最终result。
前端看不到AI调了什么工具。

修复：非流式模式返回时，把工具调用过程也放进response里。
Anthropic格式的response.content数组里本来就会包含tool_use块，
只是现在循环结束后只返回了最后一轮的result。

方案：把每一轮的content拼起来返回，或者在最终result的content
数组前面插入之前的thinking和tool_use块。

参考流式的做法，确保前端能看到工具调用卡片。
修完后测试非流式下Kelivo能否看到工具调用。git commit。
