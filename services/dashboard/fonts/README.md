# Dashboard 的后备字体

颜文字常用一些很少见的文字（比如 `𖦹` 是巴姆穆文、`ࡇ` 是曼达文、`꒰` 是彝文、`՞` 是亚美尼亚文、`ܸ` 是叙利亚文、`ᐝ` 是加拿大原住民音节文字）。
很多电脑和手机没有这些字的字体，就会显示成方框。这里放了这几种文字的 Noto 字体（只含那一种文字的字），
Dashboard 的 CSS 里用 `unicode-range` 引用：页面上出现这些字、而且系统字体里没有时才会下载，平时不加载。

- 来源：Google Noto 字体，取自 [Fontsource](https://fontsource.org) 5.3.0 的 npm 包（`@fontsource/noto-sans-*`，400 字重，各文字自己的子集）。
- 许可：SIL Open Font License 1.1，见同目录的 `LICENSE-*.txt`。
