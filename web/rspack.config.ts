import path from 'node:path'
import { rspack } from '@rspack/core'
import type { Configuration } from '@rspack/core'

const API_ORIGIN = process.env.WEB_API_ORIGIN || 'http://127.0.0.1:8088'
const PORT = Number(process.env.CHAT_WEB_PORT || 8090)

const config: Configuration = {
  mode: process.env.NODE_ENV === 'production' ? 'production' : 'development',
  entry: { main: './src/main.tsx' },
  output: {
    path: path.resolve(process.cwd(), 'dist'),
    filename: '[name].[contenthash].js',
    publicPath: '/',
    clean: true,
  },
  resolve: {
    extensions: ['.tsx', '.ts', '.jsx', '.js', '.json'],
    // 源码用 ESM 的 .js 后缀引用 .ts/.tsx（NodeNext 风格），交给打包器做映射。
    extensionAlias: {
      '.js': ['.ts', '.tsx', '.js'],
    },
  },
  module: {
    rules: [
      {
        test: /\.(tsx?|jsx?)$/,
        exclude: /node_modules/,
        use: {
          loader: 'builtin:swc-loader',
          options: {
            jsc: {
              parser: { syntax: 'typescript', tsx: true },
              transform: { react: { runtime: 'automatic' } },
            },
          },
        },
      },
      {
        test: /\.module\.css$/,
        type: 'css/module',
        parser: {
          namedExports: false,
        },
        generator: {
          exportsOnly: false,
          localIdentName: '[name]__[local]--[hash:base64:5]',
        },
      },
      {
        test: /\.css$/,
        exclude: /\.module\.css$/,
        type: 'css',
      },
      {
        test: /\.(png|jpe?g|gif|svg|webp|woff2?|ttf|eot)$/,
        type: 'asset/resource',
      },
    ],
  },
  plugins: [
    new rspack.HtmlRspackPlugin({ template: './src/index.html' }),
    new rspack.DefinePlugin({ __API_ORIGIN__: JSON.stringify(API_ORIGIN) }),
  ],
  devServer: {
    host: '127.0.0.1',
    port: PORT,
    historyApiFallback: true,
    hot: true,
    client: { overlay: true },
  },
  devtool: 'source-map',
}

export default config
